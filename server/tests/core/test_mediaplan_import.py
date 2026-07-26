"""toorow -- tests for mediaplan_import.py (Story 22.2, FR38 / CAP-26).

OFFLINE FIRST: every fixture workbook is built PROGRAMMATICALLY in-memory with
openpyxl (no committed binaries). The offline suite proves the parser contract
(merged-cell resolution, forward-fill bounded to a range, the merged-budget
collapse-to-one-line rule, explicit explode, noise exclusion, FR dates/budgets,
duplicate line_key, corrupt file). The live-Postgres suite (opt-in, same pattern
and teardown as test_mediaplan_store.py) proves the end-to-end candidate + report
+ re-import unchanged + publish/diff.
"""

from __future__ import annotations

import io
import os
import uuid
from decimal import Decimal

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

openpyxl = pytest.importorskip("openpyxl")


# ---------------------------------------------------------------------------
# In-memory workbook builders (no committed binaries -- AI-54 spirit)
# ---------------------------------------------------------------------------


def _wb_bytes(build) -> bytes:
    """Run ``build(wb)`` on a fresh workbook and return its xlsx bytes."""
    wb = openpyxl.Workbook()
    # Remove the default sheet so tests fully control sheet names/order.
    default = wb.active
    wb.remove(default)
    build(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write_rows(ws, rows: list[list]) -> None:
    for r, row in enumerate(rows, start=1):
        for c, val in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=val)


# ---------------------------------------------------------------------------
# Offline tests
# ---------------------------------------------------------------------------


def test_simple_sheet_three_clean_lines():
    from core.mediaplan_import import parse_workbook

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Support", "Début", "Fin", "Budget"],
                ["Meta prospecting", "Meta", "2026-03-01", "2026-03-31", 3000],
                ["Google Search", "Google", "2026-03-01", "2026-03-31", 2000],
                ["TikTok", "TikTok", "2026-03-10", "2026-03-20", 1000],
            ],
        )

    contract = {
        "header_row": 1,
        "columns": {
            "Libellé": "label",
            "Support": "channel",
            "Début": "start_date",
            "Fin": "end_date",
            "Budget": "budget",
        },
        "line_key": "auto",
    }
    preview = parse_workbook(_wb_bytes(build), {"Digital": contract})

    assert len(preview.lines) == 3
    assert preview.rejected == []
    assert preview.sum_file == preview.sum_imported == Decimal("6000.00")
    assert {ln["channel"] for ln in preview.lines} == {"Meta", "Google", "TikTok"}


def test_merged_descriptive_forward_fill_bounded_to_range():
    from core.mediaplan_import import parse_workbook

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Support", "Début", "Fin", "Budget"],
                ["Ligne A", "Meta", "2026-03-01", "2026-03-31", 1000],
                ["Ligne B", None, "2026-03-01", "2026-03-31", 1500],
                ["Ligne C", None, "2026-03-01", "2026-03-31", 2000],
                ["Ligne D", "Google", "2026-03-01", "2026-03-31", 500],
            ],
        )
        # Merge the Support column over rows 2..4 (Meta forward-fills to B and C).
        ws.merge_cells("B2:B4")

    contract = {
        "header_row": 1,
        "columns": {
            "Libellé": "label",
            "Support": "channel",
            "Début": "start_date",
            "Fin": "end_date",
            "Budget": "budget",
        },
        "line_key": "auto",
    }
    preview = parse_workbook(_wb_bytes(build), {"Digital": contract})

    by_label = {ln["label"]: ln for ln in preview.lines}
    assert by_label["Ligne A"]["channel"] == "Meta"
    assert by_label["Ligne B"]["channel"] == "Meta"  # forward-filled
    assert by_label["Ligne C"]["channel"] == "Meta"  # forward-filled
    assert by_label["Ligne D"]["channel"] == "Google"  # NOT filled beyond range
    # 4 distinct budgets, distinct lines (descriptive merge != budget merge).
    assert len(preview.lines) == 4
    assert preview.sum_file == preview.sum_imported == Decimal("5000.00")


def test_merged_budget_collapses_to_one_line_never_multiplied():
    """LE test budget fusionné: 9000 merged over 3 sub-rows -> ONE line at 9000."""
    from core.mediaplan_import import parse_workbook

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Support", "Début", "Fin", "Budget"],
                ["Sous-ligne 1", "Meta", "2026-03-01", "2026-03-10", 9000],
                ["Sous-ligne 2", "Meta", "2026-03-11", "2026-03-20", None],
                ["Sous-ligne 3", "Meta", "2026-03-21", "2026-03-31", None],
            ],
        )
        # Budget cell merged over the 3 physical sub-rows.
        ws.merge_cells("E2:E4")

    contract = {
        "header_row": 1,
        "columns": {
            "Libellé": "label",
            "Support": "channel",
            "Début": "start_date",
            "Fin": "end_date",
            "Budget": "budget",
        },
        "line_key": "auto",
    }
    preview = parse_workbook(_wb_bytes(build), {"Digital": contract})

    assert len(preview.lines) == 1, "merged budget must collapse to ONE line"
    line = preview.lines[0]
    assert line["budget"] == "9000.00", "budget must NOT be multiplied (never 27000)"
    # Span = (min start, max end) across the group.
    assert line["start_date"] == "2026-03-01"
    assert line["end_date"] == "2026-03-31"
    # The 3 sub-labels are transported as detail (report), not lost.
    assert line["detail"] == ["Sous-ligne 1", "Sous-ligne 2", "Sous-ligne 3"]
    assert preview.sum_file == preview.sum_imported == Decimal("9000.00")


def test_explicit_explode_even_split_makes_three_lines():
    from core.mediaplan_import import parse_workbook

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Début", "Fin", "Budget"],
                ["Sous 1", "2026-03-01", "2026-03-10", 9000],
                ["Sous 2", "2026-03-11", "2026-03-20", None],
                ["Sous 3", "2026-03-21", "2026-03-31", None],
            ],
        )
        ws.merge_cells("D2:D4")

    contract = {
        "header_row": 1,
        "columns": {
            "Libellé": "label",
            "Début": "start_date",
            "Fin": "end_date",
            "Budget": "budget",
        },
        "line_key": "auto",
        "explode_merged_budget": {"D2:D4": [3000, 3000, 3000]},
    }
    preview = parse_workbook(_wb_bytes(build), {"Digital": contract})

    assert len(preview.lines) == 3
    assert [ln["budget"] for ln in preview.lines] == ["3000.00", "3000.00", "3000.00"]
    assert preview.sum_file == preview.sum_imported == Decimal("9000.00")


def test_explicit_explode_bad_split_is_422():
    from core.mediaplan_import import MediaPlanValidationError, parse_workbook

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Début", "Fin", "Budget"],
                ["Sous 1", "2026-03-01", "2026-03-10", 9000],
                ["Sous 2", "2026-03-11", "2026-03-20", None],
                ["Sous 3", "2026-03-21", "2026-03-31", None],
            ],
        )
        ws.merge_cells("D2:D4")

    contract = {
        "header_row": 1,
        "columns": {
            "Libellé": "label",
            "Début": "start_date",
            "Fin": "end_date",
            "Budget": "budget",
        },
        "line_key": "auto",
        "explode_merged_budget": {"D2:D4": [3000, 3000, 2000]},  # sums to 8000 != 9000
    }
    with pytest.raises(MediaPlanValidationError):
        parse_workbook(_wb_bytes(build), {"Digital": contract})


def test_subtotal_row_without_dates_is_rejected_with_reason():
    from core.mediaplan_import import parse_workbook

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Début", "Fin", "Budget"],
                ["Meta", "2026-03-01", "2026-03-31", 3000],
                ["TOTAL", None, None, 3000],  # decorative sub-total: no dates
            ],
        )

    contract = {
        "header_row": 1,
        "columns": {
            "Libellé": "label",
            "Début": "start_date",
            "Fin": "end_date",
            "Budget": "budget",
        },
        "line_key": "auto",
    }
    preview = parse_workbook(_wb_bytes(build), {"Digital": contract})

    assert len(preview.lines) == 1
    assert len(preview.rejected) == 1
    assert preview.rejected[0]["label"] == "TOTAL"
    assert "dates" in preview.rejected[0]["raison"]
    # The TOTAL row carries a READABLE budget (3000) but is rejected for its dates:
    # it lands in sum_rejected, not silently melted. The invariant is file ==
    # imported + rejected, with rejected > 0 (F-1: sum_file is computed
    # INDEPENDENTLY of the normalisation path).
    assert preview.sum_imported == Decimal("3000.00")
    assert preview.sum_rejected == Decimal("3000.00")
    assert preview.sum_file == Decimal("6000.00")
    assert preview.sum_file == preview.sum_imported + preview.sum_rejected
    assert preview.rejected[0]["budget"] == "3000.00"


def test_multi_sheet_tv_plan_only_and_digital_in_one_op():
    from core.mediaplan_import import parse_workbook

    def build(wb):
        tv = wb.create_sheet("TV")
        _write_rows(
            tv,
            [
                ["Campagne", "Du", "Au", "Montant"],
                ["TV nationale", "2026-03-01", "2026-03-31", 50000],
            ],
        )
        dg = wb.create_sheet("Digital")
        _write_rows(
            dg,
            [
                ["Libellé", "Début", "Fin", "Budget"],
                ["Meta", "2026-03-01", "2026-03-31", 3000],
            ],
        )

    contracts = {
        "TV": {
            "header_row": 1,
            "columns": {
                "Campagne": "label",
                "Du": "start_date",
                "Au": "end_date",
                "Montant": "budget",
            },
            "line_key": "auto",
            "is_plan_only": True,  # plan-only via contract
        },
        "Digital": {
            "header_row": 1,
            "columns": {
                "Libellé": "label",
                "Début": "start_date",
                "Fin": "end_date",
                "Budget": "budget",
            },
            "line_key": "auto",
        },
    }
    preview = parse_workbook(_wb_bytes(build), contracts)

    assert len(preview.lines) == 2
    by_label = {ln["label"]: ln for ln in preview.lines}
    assert by_label["TV nationale"]["is_plan_only"] is True
    assert by_label["Meta"]["is_plan_only"] is False
    assert preview.sum_file == preview.sum_imported == Decimal("53000.00")


def test_fr_dates_and_fr_budget_string():
    from core.mediaplan_import import parse_workbook

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Début", "Fin", "Budget"],
                ["Meta", "01/03/2026", "31/03/2026", "1 234,56"],
            ],
        )

    contract = {
        "header_row": 1,
        "columns": {
            "Libellé": "label",
            "Début": "start_date",
            "Fin": "end_date",
            "Budget": "budget",
        },
        "line_key": "auto",
        "date_format": "%d/%m/%Y",
    }
    preview = parse_workbook(_wb_bytes(build), {"Digital": contract})

    assert len(preview.lines) == 1
    line = preview.lines[0]
    assert line["start_date"] == "2026-03-01"
    assert line["end_date"] == "2026-03-31"
    assert line["budget"] == "1234.56"
    assert preview.sum_file == preview.sum_imported == Decimal("1234.56")


def test_duplicate_line_key_is_422():
    from core.mediaplan_import import MediaPlanValidationError, parse_workbook

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Clé", "Libellé", "Début", "Fin", "Budget"],
                ["meta", "Meta A", "2026-03-01", "2026-03-31", 1000],
                ["meta", "Meta B", "2026-03-01", "2026-03-31", 2000],  # dup key
            ],
        )

    contract = {
        "header_row": 1,
        "columns": {
            "Libellé": "label",
            "Début": "start_date",
            "Fin": "end_date",
            "Budget": "budget",
        },
        "line_key": "Clé",
    }
    with pytest.raises(MediaPlanValidationError):
        parse_workbook(_wb_bytes(build), {"Digital": contract})


def test_corrupt_file_raises_typed_error():
    from core.mediaplan_import import MediaPlanImportError, parse_workbook

    contract = {
        "header_row": 1,
        "columns": {"Budget": "budget"},
        "line_key": "auto",
    }
    with pytest.raises(MediaPlanImportError):
        parse_workbook(b"\x00\x01\x02not-an-xlsx", {"Digital": contract})


def test_column_letters_instead_of_headers():
    """Contract may address columns by letter (A/B/...) not just header text."""
    from core.mediaplan_import import parse_workbook

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["x", "x", "x", "x"],  # header row 1 (unused text)
                ["Meta", "2026-03-01", "2026-03-31", 3000],
            ],
        )

    contract = {
        "header_row": 1,
        "columns": {"A": "label", "B": "start_date", "C": "end_date", "D": "budget"},
        "line_key": "auto",
    }
    preview = parse_workbook(_wb_bytes(build), {"Digital": contract})
    assert len(preview.lines) == 1
    assert preview.lines[0]["label"] == "Meta"


# ---------------------------------------------------------------------------
# F-1: sum_file is computed INDEPENDENTLY -> the invariant can catch a
# merge double-count (not a tautology x == x).
# ---------------------------------------------------------------------------


def test_sum_file_is_independent_rejected_budget_is_surfaced_not_melted():
    """A row rejected for bad DATES but with a READABLE budget lands in sum_rejected.

    Because sum_file is a separate pass over the budget column, it counts the
    rejected row's budget too -> file == imported + rejected, rejected > 0. If
    sum_file were derived from the normalisation path it could never differ.
    """
    from core.mediaplan_import import parse_workbook

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Début", "Fin", "Budget"],
                ["Meta", "2026-03-01", "2026-03-31", 3000],
                ["Ghost", "pas-une-date", "aussi-pas", 1500],  # readable budget, bad dates
            ],
        )

    contract = {
        "header_row": 1,
        "columns": {
            "Libellé": "label",
            "Début": "start_date",
            "Fin": "end_date",
            "Budget": "budget",
        },
        "line_key": "auto",
    }
    preview = parse_workbook(_wb_bytes(build), {"Digital": contract})

    assert len(preview.lines) == 1
    assert preview.sum_imported == Decimal("3000.00")
    assert preview.sum_rejected == Decimal("1500.00")
    assert preview.sum_file == Decimal("4500.00")
    assert preview.sum_file == preview.sum_imported + preview.sum_rejected
    # The rejected budget is visible in the report, never melted.
    assert preview.rejected[0]["budget"] == "1500.00"


def test_invariant_raises_when_normalisation_double_counts_a_merge(monkeypatch):
    """Discriminant test: force the normalisation path to double-count a merged
    budget (as if a merge were forward-filled per row). sum_file (independent)
    stays honest, so the invariant DIVERGES and raises -- proving it is not x == x.
    """
    import core.mediaplan_import as mpi

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Début", "Fin", "Budget"],
                ["Sous 1", "2026-03-01", "2026-03-10", 9000],
                ["Sous 2", "2026-03-11", "2026-03-20", None],
                ["Sous 3", "2026-03-21", "2026-03-31", None],
            ],
        )
        ws.merge_cells("D2:D4")  # merged budget over 3 sub-rows

    contract = {
        "header_row": 1,
        "columns": {
            "Libellé": "label",
            "Début": "start_date",
            "Fin": "end_date",
            "Budget": "budget",
        },
        "line_key": "auto",
    }

    # Sabotage the collapse: make the merged group emit N lines each at the FULL
    # merged budget (a forward-fill bug). sum_imported becomes 3 * 9000 while the
    # independent sum_file stays 9000 -> invariant must raise.
    real_group = mpi._parse_budget_group

    def buggy_group(*args, **kwargs):
        preview = kwargs["preview"]
        preview.sum_imported += Decimal("18000.00")  # extra double-count
        return real_group(*args, **kwargs)

    monkeypatch.setattr(mpi, "_parse_budget_group", buggy_group)

    with pytest.raises(mpi.MediaPlanImportError) as exc:
        mpi.parse_workbook(_wb_bytes(build), {"Digital": contract})
    assert "Invariant" in str(exc.value)


# ---------------------------------------------------------------------------
# F-2: explode weight negative or null -> immediate reject, precise message.
# ---------------------------------------------------------------------------


def test_explode_negative_weight_is_rejected_at_parsing():
    from core.mediaplan_import import MediaPlanValidationError, parse_workbook

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Début", "Fin", "Budget"],
                ["Sous 1", "2026-03-01", "2026-03-10", 9000],
                ["Sous 2", "2026-03-11", "2026-03-20", None],
                ["Sous 3", "2026-03-21", "2026-03-31", None],
            ],
        )
        ws.merge_cells("D2:D4")

    contract = {
        "header_row": 1,
        "columns": {
            "Libellé": "label",
            "Début": "start_date",
            "Fin": "end_date",
            "Budget": "budget",
        },
        "line_key": "auto",
        "explode_merged_budget": {"D2:D4": [12000, -3000, 0]},  # negative + null
    }
    with pytest.raises(MediaPlanValidationError) as exc:
        parse_workbook(_wb_bytes(build), {"Digital": contract})
    assert "poids négatif" in str(exc.value)


# ---------------------------------------------------------------------------
# F-4: a fully-noise file (only sub-totals) -> MediaPlanImportError, no line.
# ---------------------------------------------------------------------------


def test_all_noise_file_yields_no_line_and_raises_on_import():
    """Offline: parse_workbook produces ZERO lines when every row is noise; the
    import orchestration then refuses (no candidate created)."""
    from core.mediaplan_import import parse_workbook

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Début", "Fin", "Budget"],
                ["SOUS-TOTAL 1", None, None, 3000],
                ["SOUS-TOTAL 2", None, None, 2000],
                ["TOTAL GÉNÉRAL", None, None, 5000],
            ],
        )

    contract = {
        "header_row": 1,
        "columns": {
            "Libellé": "label",
            "Début": "start_date",
            "Fin": "end_date",
            "Budget": "budget",
        },
        "line_key": "auto",
    }
    preview = parse_workbook(_wb_bytes(build), {"Digital": contract})

    assert preview.lines == []
    assert len(preview.rejected) == 3
    # Every readable budget is surfaced (invariant holds), none imported.
    assert preview.sum_imported == Decimal("0.00")
    assert preview.sum_rejected == Decimal("10000.00")
    assert preview.sum_file == preview.sum_imported + preview.sum_rejected


# ---------------------------------------------------------------------------
# F-5: DoS bounds -- absurd sheet dimensions raise a clean typed error.
# ---------------------------------------------------------------------------


def test_absurd_sheet_dimensions_raise_clean_error(monkeypatch):
    from core.mediaplan_import import MediaPlanImportError, parse_workbook

    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Début", "Fin", "Budget"],
                ["Meta", "2026-03-01", "2026-03-31", 3000],
            ],
        )

    contract = {
        "header_row": 1,
        "columns": {
            "Libellé": "label",
            "Début": "start_date",
            "Fin": "end_date",
            "Budget": "budget",
        },
        "line_key": "auto",
    }
    # Lower the bound so the small fixture trips it (proves the guard fires BEFORE
    # the per-cell loops, without building a 100k-row workbook).
    import core.mediaplan_import as mpi

    monkeypatch.setattr(mpi, "MAX_SHEET_ROWS", 1)
    with pytest.raises(MediaPlanImportError) as exc:
        parse_workbook(_wb_bytes(build), {"Digital": contract})
    assert "trop grande" in str(exc.value)


# ---------------------------------------------------------------------------
# F-6: validate_contract rejects bad explode weights at the PUT (not only import).
# ---------------------------------------------------------------------------


def test_validate_contract_rejects_negative_explode_weight():
    from core.mediaplan_import import MediaPlanValidationError, validate_contract

    contract = {
        "header_row": 1,
        "columns": {"Budget": "budget"},
        "line_key": "auto",
        "explode_merged_budget": {"D2:D4": [3000, -1, 2000]},
    }
    with pytest.raises(MediaPlanValidationError) as exc:
        validate_contract(contract)
    assert "poids négatif" in str(exc.value)


def test_validate_contract_rejects_non_numeric_explode_weight():
    from core.mediaplan_import import MediaPlanValidationError, validate_contract

    contract = {
        "header_row": 1,
        "columns": {"Budget": "budget"},
        "line_key": "auto",
        "explode_merged_budget": {"D2:D4": ["abc", 2000]},
    }
    with pytest.raises(MediaPlanValidationError) as exc:
        validate_contract(contract)
    assert "invalide" in str(exc.value)


# ---------------------------------------------------------------------------
# Live-Postgres suite (opt-in) -- end-to-end candidate + report + re-import + diff
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")


def _connect():
    import psycopg

    return psycopg.connect(os.environ["TEST_POSTGRES_DSN"])


def _seed_project(conn) -> str:
    project_id = f"proj-mpi-{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, status, currency, timezone, created_by,
                org_id)
            VALUES (%s, %s, %s, 'active', 'EUR', 'Europe/Paris', 'system', 'org_test_fixture')
            """,
            (project_id, "MPI Test", project_id),
        )
    conn.commit()
    return project_id


def _drop_project(conn, project_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE app.plan_allocation_daily DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.media_plan_versions DISABLE TRIGGER USER")
        try:
            cur.execute(
                """
                DELETE FROM app.plan_allocation_daily WHERE version_id IN (
                    SELECT v.id FROM app.media_plan_versions v
                    JOIN app.media_plans p ON p.id = v.plan_id WHERE p.project_id = %s
                )
                """,
                (project_id,),
            )
            cur.execute(
                """
                DELETE FROM app.media_plan_lines WHERE version_id IN (
                    SELECT v.id FROM app.media_plan_versions v
                    JOIN app.media_plans p ON p.id = v.plan_id WHERE p.project_id = %s
                )
                """,
                (project_id,),
            )
            cur.execute(
                "DELETE FROM app.media_plan_import_contracts WHERE plan_id IN "
                "(SELECT id FROM app.media_plans WHERE project_id = %s)",
                (project_id,),
            )
            cur.execute(
                """
                DELETE FROM app.media_plan_versions WHERE plan_id IN (
                    SELECT id FROM app.media_plans WHERE project_id = %s
                )
                """,
                (project_id,),
            )
            cur.execute("DELETE FROM app.media_plans WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM app.audit_log WHERE identity = 'tester'")
            cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
        finally:
            cur.execute("ALTER TABLE app.plan_allocation_daily ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.media_plan_versions ENABLE TRIGGER USER")
    conn.commit()


def _set_contract(conn, plan_id: str, sheet_name: str, contract: dict) -> None:
    import json

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.media_plan_import_contracts
                (plan_id, sheet_name, contract, created_by, created_at, updated_at)
            VALUES (%s, %s, %s, 'tester', now(), now())
            ON CONFLICT (plan_id, sheet_name) DO UPDATE
                SET contract = EXCLUDED.contract, updated_at = now()
            """,
            (plan_id, sheet_name, json.dumps(contract)),
        )
    conn.commit()


def _digital_wb() -> bytes:
    def build(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Support", "Début", "Fin", "Budget"],
                ["Meta prospecting", "Meta", "2026-03-01", "2026-03-31", 3000],
                ["Google Search", "Google", "2026-03-01", "2026-03-31", 2000],
                ["TOTAL", None, None, 5000],  # sub-total -> rejected
            ],
        )

    return _wb_bytes(build)


_DIGITAL_CONTRACT = {
    "header_row": 1,
    "columns": {
        "Libellé": "label",
        "Support": "channel",
        "Début": "start_date",
        "Fin": "end_date",
        "Budget": "budget",
    },
    "line_key": "auto",
}


@pg_available
def test_import_creates_candidate_with_report():
    from core.mediaplan_import import import_workbook
    from core.mediaplan_store import create_plan

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = create_plan(conn, project_id=project_id, name="Plan Imp", created_by="tester")
        _set_contract(conn, plan["id"], "Digital", _DIGITAL_CONTRACT)

        report = import_workbook(conn, plan_id=plan["id"], file_bytes=_digital_wb(), actor="tester")
        conn.commit()

        assert report["version"]["status"] == "candidate"
        assert set(report["per_line"]["created"]) == {
            "digital/meta-prospecting",
            "digital/google-search",
        }
        assert report["per_line"]["updated"] == []
        assert report["per_line"]["unchanged"] == []
        assert len(report["rejected"]) == 1  # the TOTAL sub-total
        # The TOTAL row's readable 5000 budget is rejected (bad dates), surfaced in
        # sums.rejected, never melted: file == imported + rejected (F-1).
        assert report["sums"]["imported"] == "5000.00"
        assert report["sums"]["rejected"] == "5000.00"
        assert report["sums"]["file"] == "10000.00"
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_reimport_same_file_is_unchanged_everywhere():
    from core.mediaplan_import import import_workbook
    from core.mediaplan_store import create_plan, publish_version

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = create_plan(conn, project_id=project_id, name="Plan Re", created_by="tester")
        _set_contract(conn, plan["id"], "Digital", _DIGITAL_CONTRACT)

        first = import_workbook(conn, plan_id=plan["id"], file_bytes=_digital_wb(), actor="tester")
        publish_version(conn, version_id=first["version"]["id"], published_by="tester")
        conn.commit()

        # Re-import the SAME file: everything unchanged vs the now-active version.
        second = import_workbook(conn, plan_id=plan["id"], file_bytes=_digital_wb(), actor="tester")
        conn.commit()
        assert second["per_line"]["created"] == []
        assert second["per_line"]["updated"] == []
        assert set(second["per_line"]["unchanged"]) == {
            "digital/meta-prospecting",
            "digital/google-search",
        }
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_import_publish_then_changed_reimport_diffs():
    from core.mediaplan_import import import_workbook
    from core.mediaplan_store import create_plan, diff_versions, publish_version

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = create_plan(conn, project_id=project_id, name="Plan Diff", created_by="tester")
        _set_contract(conn, plan["id"], "Digital", _DIGITAL_CONTRACT)

        first = import_workbook(conn, plan_id=plan["id"], file_bytes=_digital_wb(), actor="tester")
        publish_version(conn, version_id=first["version"]["id"], published_by="tester")
        conn.commit()

        # Next month: Meta budget bumped 3000 -> 4000.
        def build(wb):
            ws = wb.create_sheet("Digital")
            _write_rows(
                ws,
                [
                    ["Libellé", "Support", "Début", "Fin", "Budget"],
                    ["Meta prospecting", "Meta", "2026-03-01", "2026-03-31", 4000],
                    ["Google Search", "Google", "2026-03-01", "2026-03-31", 2000],
                ],
            )

        second = import_workbook(
            conn, plan_id=plan["id"], file_bytes=_wb_bytes(build), actor="tester"
        )
        conn.commit()

        assert set(second["per_line"]["updated"]) == {"digital/meta-prospecting"}
        assert set(second["per_line"]["unchanged"]) == {"digital/google-search"}

        diff = diff_versions(
            conn, version_a=first["version"]["id"], version_b=second["version"]["id"]
        )
        changed = {c["line_key"] for c in diff["changed"]}
        assert changed == {"digital/meta-prospecting"}
        assert "budget" in diff["changed"][0]["changes"]
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_corrupt_file_leaves_published_version_intact():
    from core.mediaplan_import import MediaPlanImportError, import_workbook
    from core.mediaplan_store import create_plan, publish_version

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = create_plan(conn, project_id=project_id, name="Plan Safe", created_by="tester")
        _set_contract(conn, plan["id"], "Digital", _DIGITAL_CONTRACT)
        first = import_workbook(conn, plan_id=plan["id"], file_bytes=_digital_wb(), actor="tester")
        publish_version(conn, version_id=first["version"]["id"], published_by="tester")
        conn.commit()

        with pytest.raises(MediaPlanImportError):
            import_workbook(
                conn, plan_id=plan["id"], file_bytes=b"garbage-not-xlsx", actor="tester"
            )
        conn.rollback()

        # The active version is still the first one; no new candidate exists.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app.media_plan_versions WHERE plan_id = %s",
                (plan["id"],),
            )
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT id FROM app.media_plan_versions WHERE plan_id = %s AND is_active",
                (plan["id"],),
            )
            assert cur.fetchone()[0] == first["version"]["id"]
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_all_noise_file_creates_no_candidate_version():
    """F-4 (live): a 100%-noise file (only sub-totals) refuses the import and
    creates NO candidate version -- the published version stays intact."""
    from core.mediaplan_import import MediaPlanImportError, import_workbook
    from core.mediaplan_store import create_plan, publish_version

    def noise_only(wb):
        ws = wb.create_sheet("Digital")
        _write_rows(
            ws,
            [
                ["Libellé", "Support", "Début", "Fin", "Budget"],
                ["SOUS-TOTAL", None, None, None, 3000],
                ["TOTAL GÉNÉRAL", None, None, None, 5000],
            ],
        )

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = create_plan(conn, project_id=project_id, name="Plan Noise", created_by="tester")
        _set_contract(conn, plan["id"], "Digital", _DIGITAL_CONTRACT)
        first = import_workbook(conn, plan_id=plan["id"], file_bytes=_digital_wb(), actor="tester")
        publish_version(conn, version_id=first["version"]["id"], published_by="tester")
        conn.commit()

        with pytest.raises(MediaPlanImportError):
            import_workbook(
                conn, plan_id=plan["id"], file_bytes=_wb_bytes(noise_only), actor="tester"
            )
        conn.rollback()

        # No candidate created: still exactly the one published version.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app.media_plan_versions WHERE plan_id = %s",
                (plan["id"],),
            )
            assert cur.fetchone()[0] == 1
    finally:
        _drop_project(conn, project_id)
        conn.close()
