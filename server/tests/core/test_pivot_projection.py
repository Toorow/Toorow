"""A pivot rearranges an answer; it never computes a new one (story 66.6).

Pure, and it can be: the projection takes an immutable Result's schema and rows.
What has to be proven is arithmetic and refusal, and both are decided by this
module alone -- no database can make a ratio fold correctly.
"""

from __future__ import annotations

import pytest
from core.pivot_projection import (
    MAX_CELLS,
    MAX_RESPONSE_BYTES,
    MAX_ROW_KEYS,
    NO_CONTRIBUTING_ROW,
    PivotRefused,
    canonical_bytes,
    project,
)

SCHEMA = {
    "fields": [
        {"name": "k_day", "canonical_field_id": "mdm_day", "role": "dimension"},
        {"name": "k_campaign", "canonical_field_id": "mdm_campaign", "role": "dimension"},
        {
            "name": "m_spend",
            "canonical_field_id": "mdm_spend",
            "role": "measure",
            "aggregation": "sum",
            "datastream_id": "ds_left",
        },
        {
            "name": "m_revenue",
            "canonical_field_id": "mdm_revenue",
            "role": "measure",
            "aggregation": "sum",
            "datastream_id": "ds_right",
        },
        {
            "name": "r_roas",
            "canonical_field_id": "mdm_roas",
            "role": "ratio",
            "numerator_field_id": "mdm_revenue",
            "denominator_field_id": "mdm_spend",
        },
    ]
}

ROWS = [
    {"k_day": "2026-08-01", "k_campaign": "A", "m_spend": 10, "m_revenue": 100, "r_roas": 10.0},
    {"k_day": "2026-08-01", "k_campaign": "B", "m_spend": 40, "m_revenue": 40, "r_roas": 1.0},
    {"k_day": "2026-08-02", "k_campaign": "A", "m_spend": 8, "m_revenue": 200, "r_roas": 25.0},
]


def _project(**request):
    base = {"rows": ["k_campaign"], "columns": ["k_day"], "values": ["m_spend"]}
    base.update(request)
    return project(
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        schema=SCHEMA,
        rows=ROWS,
        request=base,
    )


def _cell(matrix, row_key, column_key):
    for cell in matrix["cells"]:
        if cell["row_key"] == [row_key] and cell["column_key"] == [column_key]:
            return cell
    raise AssertionError(f"no cell at {row_key} x {column_key}")


# ---------------------------------------------------------------------------
# The matrix is the Result, rearranged
# ---------------------------------------------------------------------------


def test_the_matrix_carries_the_same_result_identity():
    """Table, pivot and chart are one Result; the pivot says so in its payload."""
    matrix = _project()
    assert matrix["result_id"] == "qr_EXAMPLE"
    assert matrix["content_hash"] == "a" * 64


def test_every_axis_key_appears_once_in_a_stable_order():
    matrix = _project()
    assert matrix["row_keys"] == [["A"], ["B"]]
    assert matrix["column_keys"] == [["2026-08-01"], ["2026-08-02"]]


def test_each_axis_can_reverse_its_stable_server_owned_order():
    matrix = _project(row_sort="desc", column_sort="desc")
    assert matrix["row_keys"] == [["B"], ["A"]]
    assert matrix["column_keys"] == [["2026-08-02"], ["2026-08-01"]]
    assert matrix["sort"] == {"rows": "desc", "columns": "desc"}


def test_a_cell_folds_the_rows_that_belong_to_it():
    matrix = _project()
    assert _cell(matrix, "A", "2026-08-01")["values"]["m_spend"]["value"] == 10
    assert _cell(matrix, "A", "2026-08-02")["values"]["m_spend"]["value"] == 8


def test_an_empty_cell_is_null_with_its_reason_and_never_zero():
    """`0` and 'nothing landed here' are different facts about the business."""
    matrix = _project()
    empty = _cell(matrix, "B", "2026-08-02")["values"]["m_spend"]
    assert empty["value"] is None
    assert empty["absent_reason"] == NO_CONTRIBUTING_ROW
    assert _cell(matrix, "B", "2026-08-02")["contributing_rows"] == 0


def test_null_and_empty_string_axis_values_remain_distinct() -> None:
    rows = [
        {"k_day": "d", "k_campaign": None, "m_spend": 1},
        {"k_day": "d", "k_campaign": "", "m_spend": 2},
    ]
    matrix = project(
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        schema=SCHEMA,
        rows=rows,
        request={"rows": ["k_campaign"], "columns": [], "values": ["m_spend"]},
    )

    assert matrix["row_keys"] == [[None], [""]]
    assert [cell["values"]["m_spend"]["value"] for cell in matrix["cells"]] == [1, 2]


def test_axis_and_filter_identity_preserve_json_scalar_types() -> None:
    rows = [
        {"k_day": "d", "k_campaign": None, "m_spend": 1},
        {"k_day": "d", "k_campaign": "(not set)", "m_spend": 2},
        {"k_day": "d", "k_campaign": 1, "m_spend": 3},
        {"k_day": "d", "k_campaign": "1", "m_spend": 4},
    ]
    matrix = project(
        result_id="qr_EXAMPLE",
        content_hash="b" * 64,
        schema=SCHEMA,
        rows=rows,
        request={
            "rows": ["k_campaign"],
            "columns": [],
            "values": ["m_spend"],
            "filters": [{"field": "k_campaign", "in": [1]}],
        },
    )

    assert matrix["row_keys"] == [[1]]
    assert matrix["cells"][0]["values"]["m_spend"]["value"] == 3
    assert matrix["cells"][0]["contributing_row_indexes"] == [2]
    assert matrix["bounds"]["response_bytes"] == len(canonical_bytes(matrix))


def test_axis_identity_separates_booleans_from_numbers_and_sorts_numbers_numerically() -> None:
    rows = [
        {"k_day": "d", "k_campaign": True, "m_spend": 1},
        {"k_day": "d", "k_campaign": 1, "m_spend": 2},
        {"k_day": "d", "k_campaign": 10, "m_spend": 10},
        {"k_day": "d", "k_campaign": 2, "m_spend": 2},
    ]
    matrix = project(
        result_id="qr_EXAMPLE",
        content_hash="c" * 64,
        schema=SCHEMA,
        rows=rows,
        request={"rows": ["k_campaign"], "columns": [], "values": ["m_spend"]},
    )

    assert matrix["row_keys"] == [[True], [1], [2], [10]]
    assert [cell["values"]["m_spend"]["value"] for cell in matrix["cells"]] == [1, 2, 2, 10]


def test_a_cell_names_the_result_rows_behind_it():
    matrix = _project()
    cell = _cell(matrix, "A", "2026-08-01")
    assert cell["contributing_rows"] == 1
    assert cell["contributing_row_indexes"] == [0]


def test_the_value_descriptor_carries_the_source_of_each_measure():
    matrix = _project(values=["m_spend", "m_revenue"])
    by_name = {field["name"]: field for field in matrix["value_fields"]}
    assert by_name["m_spend"]["datastream_id"] == "ds_left"
    assert by_name["m_revenue"]["datastream_id"] == "ds_right"


# ---------------------------------------------------------------------------
# Ratios: recomputed at every level, never folded
# ---------------------------------------------------------------------------


def test_a_ratio_subtotal_is_recomputed_from_its_components():
    """Campaign A: rows say 10 and 25. Their mean is 17.5; the answer is 300/18."""
    matrix = _project(values=["r_roas"], subtotals=True, columns=[])
    subtotal = {entry["row_key"][0]: entry for entry in matrix["row_subtotals"]}["A"]
    value = subtotal["values"]["r_roas"]["value"]
    assert round(value, 6) == round(300 / 18, 6)
    assert subtotal["values"]["r_roas"]["recomputed_from"] == ["m_revenue", "m_spend"]


def test_a_ratio_grand_total_is_recomputed_too():
    matrix = _project(values=["r_roas"], grand_total="both")
    total = matrix["grand_total"]["overall"]["r_roas"]["value"]
    assert round(total, 6) == round(340 / 58, 6)


def test_a_ratio_with_a_zero_denominator_is_null_and_says_why():
    rows = [{"k_day": "d", "k_campaign": "A", "m_spend": 0, "m_revenue": 5, "r_roas": None}]
    matrix = project(
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        schema=SCHEMA,
        rows=rows,
        request={"rows": ["k_campaign"], "columns": ["k_day"], "values": ["r_roas"]},
    )
    value = matrix["cells"][0]["values"]["r_roas"]
    assert value["value"] is None
    assert value["absent_reason"] == "ratio_component_missing"


# ---------------------------------------------------------------------------
# Totals of foldable measures
# ---------------------------------------------------------------------------


def test_a_row_subtotal_sums_an_additive_measure():
    matrix = _project(subtotals=True)
    subtotal = {entry["row_key"][0]: entry for entry in matrix["row_subtotals"]}["A"]
    assert subtotal["values"]["m_spend"]["value"] == 18


def test_the_grand_total_is_stated_only_when_it_was_asked_for():
    assert "grand_total" not in _project()
    assert _project(grand_total="both")["grand_total"]["overall"]["m_spend"]["value"] == 58


# ---------------------------------------------------------------------------
# Both axes total, and the policy is READ (2026-08-21)
#
# `column_subtotals` had zero occurrences in the repository, and `rows`,
# `columns` and `both` produced the same single number with the word carried
# beside it as decoration: three accepted answers, one output.
# ---------------------------------------------------------------------------


def test_subtotals_total_the_column_axis_too():
    """A matrix that totals one axis and not the other is a grouped list."""
    matrix = _project(subtotals=True)

    assert [entry["column_key"] for entry in matrix["column_subtotals"]] == matrix["column_keys"]
    by_day = {entry["column_key"][0]: entry for entry in matrix["column_subtotals"]}
    # Every day column, summed down all campaigns, sums back to the grand total.
    assert sum(entry["values"]["m_spend"]["value"] for entry in by_day.values()) == 58


def test_a_ratio_column_subtotal_is_recomputed_from_its_components_too():
    matrix = _project(values=["r_roas"], subtotals=True)
    entry = matrix["column_subtotals"][0]
    assert entry["values"]["r_roas"]["recomputed_from"] == ["m_revenue", "m_spend"]


def test_the_grand_total_policy_decides_which_axis_collapses():
    """Three accepted words, three different documents.

    `rows` collapses the rows away and leaves one total per COLUMN key;
    `columns` does the mirror; only `both` produces the corner cell, because a
    corner needs the two axes collapsed.
    """
    rows_only = _project(grand_total="rows")["grand_total"]
    assert set(rows_only) == {"policy", "by_column_key"}
    assert [entry["column_key"] for entry in rows_only["by_column_key"]]

    columns_only = _project(grand_total="columns")["grand_total"]
    assert set(columns_only) == {"policy", "by_row_key"}
    assert [entry["row_key"] for entry in columns_only["by_row_key"]]

    both = _project(grand_total="both")["grand_total"]
    assert set(both) == {"policy", "by_column_key", "by_row_key", "overall"}


def test_every_axis_total_sums_back_to_the_overall():
    """The two collapses and the corner are one arithmetic, not three."""
    grand_total = _project(grand_total="both")["grand_total"]
    overall = grand_total["overall"]["m_spend"]["value"]

    assert sum(e["values"]["m_spend"]["value"] for e in grand_total["by_column_key"]) == overall
    assert sum(e["values"]["m_spend"]["value"] for e in grand_total["by_row_key"]) == overall


# ---------------------------------------------------------------------------
# Presentation filters narrow what is shown, never what was computed
# ---------------------------------------------------------------------------


def test_a_filter_keeps_the_listed_values_and_is_echoed_back():
    matrix = _project(filters=[{"field": "k_campaign", "in": ["A"]}])
    assert matrix["row_keys"] == [["A"]]
    assert matrix["filters_applied"] == [{"field": "k_campaign", "in": ["A"]}]


def test_a_filter_on_an_unknown_field_is_refused_with_the_list_of_real_ones():
    with pytest.raises(PivotRefused) as excinfo:
        _project(filters=[{"field": "k_market", "in": ["FR"]}])
    assert excinfo.value.code == "unknown_dimension"
    assert "k_day" in excinfo.value.detail


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_pivot_cannot_ask_for_a_field_the_result_does_not_carry():
    with pytest.raises(PivotRefused) as excinfo:
        _project(rows=["k_market"])
    assert excinfo.value.code == "unknown_dimension"


def test_a_measure_in_the_rows_well_is_refused():
    with pytest.raises(PivotRefused) as excinfo:
        _project(rows=["m_spend"])
    assert excinfo.value.code == "unknown_dimension"


def test_a_pivot_with_no_value_is_refused():
    with pytest.raises(PivotRefused) as excinfo:
        _project(values=[])
    assert excinfo.value.code == "values_required"


def test_the_same_field_in_two_wells_is_refused():
    with pytest.raises(PivotRefused) as excinfo:
        _project(rows=["k_day"], columns=["k_day"])
    assert excinfo.value.code == "field_in_two_wells"


def test_an_unknown_grand_total_policy_is_refused():
    with pytest.raises(PivotRefused) as excinfo:
        _project(grand_total="sometimes")
    assert excinfo.value.code == "unknown_grand_total_policy"


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_a_wide_pivot_is_truncated_on_a_named_axis_and_says_how_much():
    rows = [
        {"k_day": f"2026-08-{day:02d}", "k_campaign": f"C{index}", "m_spend": 1}
        for day in range(1, 3)
        for index in range(600)
    ]
    matrix = project(
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        schema=SCHEMA,
        rows=rows,
        request={"rows": ["k_campaign"], "columns": ["k_day"], "values": ["m_spend"]},
    )
    assert matrix["bounds"]["rows_truncated"] is True
    assert matrix["bounds"]["rows_dropped"] == 100
    assert len(matrix["row_keys"]) == 500
    assert len(matrix["cells"]) <= MAX_CELLS


def test_the_bounds_travel_with_every_answer():
    bounds = _project()["bounds"]
    assert bounds["max_cells"] == MAX_CELLS
    assert bounds["rows_truncated"] is False


def _square(columns: int, rows: int) -> list[dict]:
    return [
        {"k_campaign": f"c{row:04d}", "k_day": f"2026-{column:03d}", "m_spend": row + column}
        for row in range(rows)
        for column in range(columns)
    ]


def _matrix(rows):
    return project(
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        schema=SCHEMA,
        rows=rows,
        request={"rows": ["k_campaign"], "columns": ["k_day"], "values": ["m_spend"]},
    )


def test_at_the_cell_cap_exactly_it_is_the_BYTE_budget_that_binds_and_it_says_so():
    """Le reste ouvert de 66.6 : la tenue A `MAX_CELLS` EXACTEMENT.

    CE QUE LES AUTRES TESTS NE COUVRAIENT PAS. Les bornes d'axe sont prouvees,
    et le budget en octets l'est sur une etiquette monstrueuse de 256 Ko. Aucun
    ne mesurait la MATRICE MAXIMALE -- la conjonction des deux plafonds, la
    forme qu'un vrai pivot atteint (des centaines de campagnes x des dizaines de
    jours), pas une etiquette pathologique.

    CE QUE LA MESURE A RENDU, ET C'EST LE POINT. 500 x 40 = 20 000 cellules,
    `MAX_CELLS` pile. Le plafond de cellules ne coupe RIEN -- on est a la
    limite, pas au-dela. La page servie porte pourtant **46 lignes**, parce que
    le BUDGET EN OCTETS a mordu a 255 440 sur 262 144. Autrement dit
    `bounds.max_cells` est annonce dans chaque reponse et n'est jamais le
    plafond atteint : un appelant qui dimensionne sa demande sur 20 000 cellules
    en recoit environ 1 840.

    Trois champs laissaient le DEDUIRE (`cells_truncated` faux + `rows_truncated`
    vrai + `response_bytes`). Aucun ne le DISAIT. `truncation_reason` le dit.
    """
    matrix = _matrix(_square(MAX_CELLS // MAX_ROW_KEYS, MAX_ROW_KEYS))
    bounds = matrix["bounds"]

    # A la limite EXACTE, le plafond de cellules n'a rien coupe.
    assert bounds["cells_truncated"] is False
    # ...et c'est l'autre plafond qui a coupe, NOMME.
    assert bounds["truncation_reason"] == "byte_budget"
    assert bounds["rows_truncated"] is True
    assert bounds["rows_dropped"] > 0

    # Le document tient son budget, mesure sur le document REEL.
    assert len(canonical_bytes(matrix)) <= MAX_RESPONSE_BYTES
    # Et il rend de quoi reprendre : une page muette se lirait comme une matrice
    # complete plus petite qu'elle n'est.
    assert bounds["next_row_cursor"]


def test_one_cell_past_the_cap_names_the_CELL_cap_instead():
    """Le jumeau : une cellule de plus, et c'est l'autre plafond qui repond.

    Sans lui, un `truncation_reason` fige sur `byte_budget` passerait le test
    ci-dessus -- << coupe par la taille >> est aussi ce que dirait un plafond de
    cellules casse.
    """
    bounds = _matrix(_square(MAX_CELLS // MAX_ROW_KEYS + 1, MAX_ROW_KEYS))["bounds"]
    assert bounds["cells_truncated"] is True
    # La PREMIERE cause est nommee, pas la derniere : le budget en octets coupe
    # aussi apres, mais ce que l'utilisateur doit reduire est la matrice.
    assert bounds["truncation_reason"] == "cell_cap"
    assert bounds["rows_dropped"] > 0


def test_a_long_axis_names_the_axis_cap_and_a_small_matrix_names_nothing():
    """Les deux autres cas, pour que les quatre soient distinguables.

    Un mot vide se lirait comme << coupe, on ne sait pas par quoi >> ; une page
    entiere doit donc porter `None`, pas une chaine.
    """
    assert _matrix(_square(2, MAX_ROW_KEYS + 100))["bounds"]["truncation_reason"] == "axis_cap"
    assert _project()["bounds"]["truncation_reason"] is None


def test_one_oversized_axis_label_is_refused_instead_of_breaking_the_byte_cap():
    rows = [{"k_day": "d" * MAX_RESPONSE_BYTES, "k_campaign": "A", "m_spend": 1}]
    with pytest.raises(PivotRefused) as excinfo:
        project(
            result_id="qr_EXAMPLE",
            content_hash="a" * 64,
            schema=SCHEMA,
            rows=rows,
            request={"rows": ["k_campaign"], "columns": ["k_day"], "values": ["m_spend"]},
        )
    assert excinfo.value.code == "pivot_row_exceeds_budget"


def test_column_axis_pages_resume_without_repeating_or_skipping_keys():
    rows = [
        {"k_campaign": "A", "k_day": f"2026-{index:03d}", "m_spend": index}
        for index in range(105)
    ]
    first = project(
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        schema=SCHEMA,
        rows=rows,
        request={"rows": ["k_campaign"], "columns": ["k_day"], "values": ["m_spend"]},
    )
    second = project(
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        schema=SCHEMA,
        rows=rows,
        request={
            "rows": ["k_campaign"],
            "columns": ["k_day"],
            "values": ["m_spend"],
            "column_offset": first["bounds"]["next_column_offset"],
        },
    )

    assert len(first["column_keys"]) == 100
    assert len(second["column_keys"]) == 5
    assert first["column_keys"][-1] < second["column_keys"][0]
    assert second["bounds"]["next_column_offset"] is None
    assert first["bounds"]["total_column_keys"] == 105


# ---------------------------------------------------------------------------
# NFR8, MEASURED: "Projection aggregates in one pass over Result rows and does
# not rescan all rows per subtotal or page trim."
#
# Until 2026-08-17 the subtotal path rebuilt its row index per row key with a
# full scan of the rows -- once per key, up to MAX_ROW_KEYS times -- and every
# test stayed green, because the answers were right. Only the COST was wrong, and
# nothing measured cost.
#
# The instrument is `bounds.row_examinations`, incremented inside `_key`, the
# primitive every row-to-bucket decision goes through. A future comprehension
# that rescans is counted whether or not its author thought about this test.
#
# Every test below holds the ROW COUNT FIXED and varies the axis. That is the
# only shape that separates "more data" from "more rescanning".
# ---------------------------------------------------------------------------

_ROWS = 300


def _rows_in_n_keys(keys: int):
    """`_ROWS` rows spread over exactly `keys` row keys."""
    return [
        {
            "k_campaign": f"C{index % keys:04d}",
            "k_day": "2026-08-01",
            "m_spend": 1,
            "m_revenue": 2,
        }
        for index in range(_ROWS)
    ]


def _projected(keys: int, **extra):
    matrix = project(
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        schema=SCHEMA,
        rows=_rows_in_n_keys(keys),
        request={
            "rows": ["k_campaign"],
            "columns": ["k_day"],
            "values": ["m_spend"],
            **extra,
        },
    )
    return matrix, matrix["bounds"]["row_examinations"]


def test_subtotals_do_not_rescan_the_rows_per_key():
    """300 rows in 1 key and 300 rows in 300 keys examine the same rows.

    The removed implementation examined all 300 rows for EVERY key: 300 examinations
    for one key and 90 000 for three hundred. Same data, 300x the work, and the
    response said nothing about it.
    """
    one, one_seen = _projected(1, subtotals=True)
    many, many_seen = _projected(300, subtotals=True)

    assert one_seen == many_seen
    # Four examinations per row, and the number is worth reading: the row axis pass,
    # the column axis pass, then the bucket pass which forms both keys again. All
    # four are bounded by the DATA; none of them by the axis.
    assert many_seen == _ROWS * 4
    # The answers are still right -- an implementation that got cheap by computing
    # less would satisfy the equality above and nothing else.
    assert len(many["row_subtotals"]) == 300
    assert all(
        entry["values"]["m_spend"]["value"] == 1 for entry in many["row_subtotals"]
    )
    assert one["row_subtotals"][0]["values"]["m_spend"]["value"] == _ROWS


def test_a_grand_total_examines_no_rows_of_its_own():
    """It folds over indexes already known, so it costs no new examination."""
    _without, without_seen = _projected(300, subtotals=True)
    _with, with_seen = _projected(300, subtotals=True, grand_total="both")

    assert with_seen == without_seen


def test_a_second_page_examines_no_more_rows_than_the_first():
    """Page trim is not a rescan."""
    _first, first_seen = _projected(300, subtotals=True)
    _second, second_seen = _projected(300, subtotals=True, row_offset=100)

    assert second_seen == first_seen


def test_more_values_do_not_multiply_the_row_examinations():
    """Values change what is folded, never how many rows are looked at."""
    _one, one_seen = _projected(300, subtotals=True, grand_total="both")
    two_values = project(
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        schema=SCHEMA,
        rows=_rows_in_n_keys(300),
        request={
            "rows": ["k_campaign"],
            "columns": ["k_day"],
            "values": ["m_spend", "m_revenue"],
            "subtotals": True,
            "grand_total": "both",
        },
    )

    assert two_values["bounds"]["row_examinations"] == one_seen


def test_the_counter_does_not_leak_between_projections():
    """Each answer reports ITS own work, not the sum since the process started."""
    _first, first_seen = _projected(300, subtotals=True)
    _second, second_seen = _projected(300, subtotals=True)

    assert first_seen == second_seen == _ROWS * 4


def test_subtotals_read_the_same_rows_the_cells_did():
    """One index, two readers -- the property the removed rescan could break.

    A subtotal built from its own scan and a cell built from the bucket map can
    disagree the moment the two ways of forming a key drift apart. Summing a row's
    cells and reading its subtotal must give one number.
    """
    rows = [
        {"k_campaign": "A", "k_day": "2026-08-01", "m_spend": 10},
        {"k_campaign": "A", "k_day": "2026-08-02", "m_spend": 8},
        {"k_campaign": "B", "k_day": "2026-08-01", "m_spend": 20},
    ]
    matrix = project(
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        schema=SCHEMA,
        rows=rows,
        request={
            "rows": ["k_campaign"],
            "columns": ["k_day"],
            "values": ["m_spend"],
            "subtotals": True,
        },
    )

    subtotal = {entry["row_key"][0]: entry for entry in matrix["row_subtotals"]}
    for campaign, expected in (("A", 18), ("B", 20)):
        from_cells = sum(
            cell["values"]["m_spend"]["value"] or 0
            for cell in matrix["cells"]
            if cell["row_key"][0] == campaign
        )
        assert from_cells == expected
        assert subtotal[campaign]["values"]["m_spend"]["value"] == expected


def test_a_total_that_spans_rows_the_page_does_not_show_says_so():
    """The right number under a table whose cells add to something smaller.

    Measured before the label: a page serving 2 row keys of 5 printed a column
    total of 50 under visible cells adding to 20. The figure is correct — a
    total spans the FILTERED SET — and the reading was not.
    """
    rows = [
        {"k_day": "2026-08-0" + str(index % 3 + 1), "k_campaign": f"C{index}", "m_spend": 10}
        for index in range(5)
    ]
    matrix = project(
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        schema=SCHEMA,
        rows=rows,
        request={
            "rows": ["k_campaign"],
            "columns": ["k_day"],
            "values": ["m_spend"],
            "subtotals": True,
            "row_offset": 3,
        },
        project_id="proj_EXAMPLE",
    )

    assert matrix["bounds"]["totals_span"] == "all_filtered_rows"
    # UNE ASSERTION QUI POUVAIT PAS ROUGIR, remplacée le 2026-08-22. Elle disait
    # `rows_dropped == 0 or totals_cover_unserved_rows`, et le drapeau valait
    # alors `bool(rows_dropped)` : `X == 0 or bool(X)` est vrai pour TOUT X. Elle
    # gardait un correctif qui ne marchait pas, sur un défaut déclaré fermé.
    assert matrix["bounds"]["totals_cover_unserved_rows"] is True
    # La page montre 2 clés de ligne sur 5 : le total en compte 5.
    assert len(matrix["row_keys"]) == 2
    assert matrix["bounds"]["total_row_keys"] == 5
    served = sum(
        cell["values"]["m_spend"]["value"] or 0
        for cell in matrix["cells"]
        if cell["values"]["m_spend"]["value"] is not None
    )
    counted = sum(entry["values"]["m_spend"]["value"] for entry in matrix["column_subtotals"])
    # Le nombre est JUSTE et la lecture ne l'était pas : c'est tout le sujet.
    assert counted > served


def test_the_flag_follows_the_head_skipped_by_the_pager_not_only_the_cut_tail():
    """Le pageur saute la TÊTE, et le premier drapeau ne comptait que la queue.

    Balayage de tous les offsets : à chacun la page montre moins que le total,
    donc à chacun l'étiquette doit apparaître. La première version rendait
    `False` partout, parce que `rows_dropped` vaut 0 quand rien n'a été coupé
    APRÈS la page.
    """
    rows = [
        {"k_day": "2026-08-01", "k_campaign": f"C{index}", "m_spend": 10}
        for index in range(5)
    ]

    def page(offset):
        return project(
            result_id="qr_EXAMPLE",
            content_hash="a" * 64,
            schema=SCHEMA,
            rows=rows,
            request={
                "rows": ["k_campaign"],
                "columns": ["k_day"],
                "values": ["m_spend"],
                "subtotals": True,
                "row_offset": offset,
            },
            project_id="proj_EXAMPLE",
        )["bounds"]

    assert page(0)["totals_cover_unserved_rows"] is False
    for offset in (1, 2, 3, 4):
        bounds = page(offset)
        assert bounds["rows_dropped"] == 0, "rien n'est coupe apres la page"
        assert bounds["totals_cover_unserved_rows"] is True, offset


def test_a_page_that_shows_everything_claims_no_unserved_rows():
    matrix = _project(subtotals=True)
    assert matrix["bounds"]["totals_span"] == "all_filtered_rows"
    assert matrix["bounds"]["totals_cover_unserved_rows"] is False
    assert matrix["bounds"]["totals_cover_unserved_columns"] is False


def test_one_axis_policies_are_arithmetic_too_not_only_the_right_keys():
    """The old pair of tests asserted key SETS and a non-empty list.

    A calculation returning the right keys with wrong numbers under `rows` alone
    or `columns` alone would have passed both.
    """
    rows_only = _project(grand_total="rows")["grand_total"]
    columns_only = _project(grand_total="columns")["grand_total"]
    overall = _project(grand_total="both")["grand_total"]["overall"]["m_spend"]["value"]

    assert sum(e["values"]["m_spend"]["value"] for e in rows_only["by_column_key"]) == overall
    assert sum(e["values"]["m_spend"]["value"] for e in columns_only["by_row_key"]) == overall
