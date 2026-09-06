"""Isolation, bounds and lifecycle for everything epic 66 added (story 66.10).

Three properties, and each one is a thing the epic can only claim once:

  * FORCE RLS. The two tables migration 258 created carry the same
    `epic36_has_resource_access` policy every scoped table of this repository
    carries. Proven by turning `toorow.enforce_epic36` ON and reading — not by
    reading the migration and believing it.
  * BOUNDS. Every producer of this epic states its caps IN its answer, and the
    pivot's byte budget is enforced on the serialized document with a cursor for
    the rest. A cap nobody can read off the response is a cap nobody can plan for.
  * NO ORPHAN WRITE. A refused compilation stores nothing; an accepted attempt
    always ends with exactly one Result.

`live_postgres` runs as the ordinary `connector` role and rolls back.
"""

from __future__ import annotations

import json

import pytest

psycopg = pytest.importorskip("psycopg")

from core import datastream_matches as matches  # noqa: E402
from core import mdm_common_keys as keys  # noqa: E402
from core import multi_source_plan as plans  # noqa: E402
from core import pivot_projection  # noqa: E402

from tests.integration.epic66_fixtures import (  # noqa: E402
    make_canonical_field,
    make_datastream,
    make_project,
)

# ---------------------------------------------------------------------------
# FORCE RLS on the tables this epic created
# ---------------------------------------------------------------------------


def test_the_new_tables_force_row_level_security(live_postgres):
    """FORCE, not just ENABLE: the owning role must not slip past its own policy."""
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = ANY(%s)",
            (["mdm_common_keys", "mdm_common_key_versions"],),
        )
        rows = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
    assert rows["mdm_common_keys"] == (True, True)
    assert rows["mdm_common_key_versions"] == (True, True)


def test_each_new_table_carries_the_project_access_policy(live_postgres):
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT tablename, qual FROM pg_policies WHERE schemaname = 'app' "
            "AND tablename = ANY(%s)",
            (["mdm_common_keys", "mdm_common_key_versions"],),
        )
        policies = {row[0]: row[1] for row in cur.fetchall()}
    assert set(policies) == {"mdm_common_keys", "mdm_common_key_versions"}
    for qual in policies.values():
        assert "epic36_has_resource_access" in qual


def test_a_foreign_project_reads_nothing_when_the_guard_is_armed(live_postgres):
    """The guard is off by default in this suite; armed, it hides the other org."""
    org_a, project_a = make_project(live_postgres, "Tenant A")
    day = make_canonical_field(live_postgres, project_a, "day", value_type="date")
    keys.create_common_key(
        live_postgres, project_id=project_a, name="Day", canonical_field_ids=[day],
        actor="tester",
    )
    with live_postgres.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.mdm_common_keys WHERE project_id = %s", (project_a,))
        assert cur.fetchone()[0] == 1

        # Armed, with no granted identity in this session, the row is invisible.
        cur.execute("SET LOCAL toorow.enforce_epic36 = 'on'")
        cur.execute("SELECT count(*) FROM app.mdm_common_keys WHERE project_id = %s", (project_a,))
        hidden = cur.fetchone()[0]
        cur.execute("SET LOCAL toorow.enforce_epic36 = 'off'")
    assert hidden == 0, "an armed FORCE RLS policy must not serve rows to an ungranted session"


def test_the_scoped_reads_never_cross_a_project_even_unarmed(live_postgres):
    """Scope is in the WHERE clause, so isolation does not depend on the guard."""
    org_a, project_a = make_project(live_postgres, "Tenant A")
    _org_b, project_b = make_project(live_postgres, "Tenant B")
    day = make_canonical_field(live_postgres, project_a, "day", value_type="date")
    created = keys.create_common_key(
        live_postgres, project_id=project_a, name="Day", canonical_field_ids=[day],
        actor="tester",
    )
    make_datastream(
        live_postgres, org_a, project_a, "A source",
        bindings={day: ("date", "confirmed")},
    )

    assert keys.list_common_keys(live_postgres, project_id=project_b) == []
    with pytest.raises(keys.CommonKeyNotFound):
        keys.read_common_key(
            live_postgres, project_id=project_b, common_key_id=created["id"]
        )
    assert matches.discover_matches(live_postgres, project_id=project_b)["matches"] == []


# ---------------------------------------------------------------------------
# Bounds: stated in the answer, enforced on the document
# ---------------------------------------------------------------------------


def test_every_producer_states_its_bounds_in_its_own_answer(live_postgres):
    """Each declared bound is in the answer -- and a new one does not fail this.

    This assertion used to be an exact `==` on three keys, so the byte budget the
    producer gained on 2026-08-15 (`max_response_bytes`, `response_bytes`, and the
    scan flag beside it) turned a legitimately ADDITIVE contract into a red test.
    NFR3 allows additions within a supported schema line; what must never happen
    is a bound going MISSING or reading a value nobody declared. So each key is
    named with its declared source, and the measured size is checked against its
    own budget instead of being pinned to a literal.
    """
    _org, project_id = make_project(live_postgres, "Bounds")
    catalog = matches.discover_matches(live_postgres, project_id=project_id)
    bounds = catalog["bounds"]
    assert bounds["max_datastreams_scanned"] == matches.MAX_DATASTREAMS_SCANNED
    assert bounds["max_matches"] == matches.MAX_MATCHES
    assert bounds["max_measures_per_datastream"] == matches.MAX_MEASURES_PER_DATASTREAM
    assert bounds["max_response_bytes"] == matches.MAX_RESPONSE_BYTES
    assert bounds["truncated"] is False
    assert bounds["datastream_scan_truncated"] is False
    assert 0 < bounds["response_bytes"] <= matches.MAX_RESPONSE_BYTES


def test_the_declared_caps_are_the_ones_the_epic_promised():
    """One place to read them, and the numbers are the acceptance criteria."""
    assert plans.MAX_MEMBERS == 4
    assert plans.MAX_MEASURES_PER_MEMBER == 20
    assert plans.MAX_DIMENSIONS == 10
    assert plans.MAX_FILTERS == 50
    assert matches.MAX_MATCHES == 50
    assert pivot_projection.MAX_CELLS == 20_000
    assert pivot_projection.MAX_ROW_KEYS == 500
    assert pivot_projection.MAX_COLUMN_KEYS == 100
    assert pivot_projection.MAX_RESPONSE_BYTES == 262_144


#: A shape a real Project reaches: 600 campaigns x 28 days x four measures. It is
#: within every count cap and still 6 MB of JSON, which is exactly why the byte
#: budget exists beside the cell cap rather than instead of it.
_MEASURES = 4


def _wide_matrix(row_count: int, request: dict | None = None):
    schema = {
        "fields": [
            {"name": "k_campaign", "canonical_field_id": "mdm_campaign", "role": "dimension"},
            {"name": "k_day", "canonical_field_id": "mdm_day", "role": "dimension"},
            *[
                {
                    "name": f"m_{index}",
                    "canonical_field_id": f"mdm_measure_{index}",
                    "role": "measure",
                    "aggregation": "sum",
                }
                for index in range(_MEASURES)
            ],
        ]
    }
    rows = [
        {
            "k_campaign": f"2026 Q3 always-on brand campaign - region {index:04d}",
            "k_day": f"2026-08-{(index % 28) + 1:02d}",
            **{f"m_{measure}": index * measure for measure in range(_MEASURES)},
        }
        for index in range(row_count)
    ]
    return pivot_projection.project(
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        schema=schema,
        rows=rows,
        request=_wide_request(request),
        project_id="proj_EXAMPLE",
    )


def _wide_request(request: dict | None = None) -> dict:
    return {
        "rows": ["k_campaign"],
        "columns": ["k_day"],
        "values": [f"m_{index}" for index in range(_MEASURES)],
        **(request or {}),
    }


def test_the_initial_response_never_exceeds_the_byte_budget():
    """The count caps do not bound bytes: this shape is inside all of them."""
    matrix = _wide_matrix(600)
    # Measured the way the wire measures it: `JSONResponse` writes compact
    # separators, so a test using the default ones would be counting a document
    # nobody sends.
    served = len(json.dumps(matrix, separators=(",", ":"), default=str).encode("utf-8"))
    assert served <= pivot_projection.MAX_RESPONSE_BYTES
    assert matrix["bounds"]["response_bytes"] <= pivot_projection.MAX_RESPONSE_BYTES


def test_what_did_not_fit_comes_back_as_a_cursor_not_as_silence():
    matrix = _wide_matrix(600)
    cursor = matrix["bounds"]["next_row_offset"]
    assert cursor is not None
    assert matrix["bounds"]["rows_truncated"] is True
    assert matrix["bounds"]["total_row_keys"] == 600

    second = _wide_matrix(600, {"row_offset": cursor})
    assert second["bounds"]["row_offset"] == cursor
    # The two pages do not overlap, because the axis order is stable.
    first_keys = {tuple(key) for key in matrix["row_keys"]}
    second_keys = {tuple(key) for key in second["row_keys"]}
    assert not (first_keys & second_keys)


def test_a_matrix_that_fits_carries_no_cursor():
    matrix = _wide_matrix(3)
    assert matrix["bounds"]["next_row_offset"] is None
    assert matrix["bounds"]["rows_truncated"] is False


def test_a_page_never_ends_mid_row():
    """Whole rows only: a half row would read as a real, smaller number."""
    matrix = _wide_matrix(600)
    served = {tuple(key) for key in matrix["row_keys"]}
    for cell in matrix["cells"]:
        assert tuple(cell["row_key"]) in served
    # Every served row carries a cell for every served column: no ragged page.
    assert len(matrix["cells"]) == len(served) * len(matrix["column_keys"])


def test_a_malformed_cursor_is_refused_rather_than_coerced():
    with pytest.raises(pivot_projection.PivotRefused) as excinfo:
        _wide_matrix(3, {"row_offset": "-2"})
    assert excinfo.value.code == "malformed_cursor"


# ---------------------------------------------------------------------------
# AC 9 -- a continuation is SIGNED and BOUND, or it is not a continuation.
#
# Until 2026-08-21 the cursor was a bare integer. Nothing tied page 2 to the
# request that produced page 1, so replaying `row_offset: 24` under another
# filter served a page of a DIFFERENT matrix -- silently, with no refusal, which
# is the "overlapping and missing pages, invisible to the caller" failure this
# story's own note forbids, arriving through the request instead of the order.
# ---------------------------------------------------------------------------


def test_a_page_that_did_not_fit_hands_back_a_signed_continuation():
    matrix = _wide_matrix(600)
    token = matrix["bounds"]["next_row_cursor"]
    assert isinstance(token, str) and "." in token
    # It carries the same offset the bare integer carries: one page, two ways of
    # naming it, and they never disagree.
    assert pivot_projection.read_cursor(
        token,
        project_id="proj_EXAMPLE",
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        request=_wide_request(),
    ) == (matrix["bounds"]["next_row_offset"], matrix["bounds"]["column_offset"])
    # The page being served names itself too, so a caller walks back without
    # keeping its own arithmetic.
    assert pivot_projection.read_cursor(
        matrix["bounds"]["cursor"],
        project_id="proj_EXAMPLE",
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        request=_wide_request(),
    ) == (matrix["bounds"]["row_offset"], matrix["bounds"]["column_offset"])


def test_a_matrix_that_fits_mints_no_continuation():
    """A cursor for a page with no successor is an invitation to nothing."""
    bounds = _wide_matrix(3)["bounds"]
    assert bounds["next_row_cursor"] is None
    assert bounds["next_column_cursor"] is None
    # It still names ITSELF: "this page" is a fact whether or not another follows.
    assert isinstance(bounds["cursor"], str)


def test_the_two_axes_page_independently():
    """One token per axis. A single `next` would move both the moment both had
    a next page, and the reader would lose a column they never asked to leave."""
    matrix = _wide_matrix(600)
    row_next = pivot_projection.read_cursor(
        matrix["bounds"]["next_row_cursor"],
        project_id="proj_EXAMPLE", result_id="qr_EXAMPLE",
        content_hash="a" * 64, request=_wide_request(),
    )
    assert row_next[1] == matrix["bounds"]["column_offset"]


def test_the_continuation_serves_the_next_page_and_does_not_overlap():
    first = _wide_matrix(600)
    second = _wide_matrix(600, {"cursor": first["bounds"]["next_row_cursor"]})

    assert second["bounds"]["row_offset"] == first["bounds"]["next_row_offset"]
    assert not (
        {tuple(key) for key in first["row_keys"]} & {tuple(key) for key in second["row_keys"]}
    )


def test_replaying_a_continuation_under_another_filter_is_refused():
    """THE DEFECT, EXACTLY. The bare offset answered this without a word."""
    first = _wide_matrix(600)
    token = first["bounds"]["next_row_cursor"]

    with pytest.raises(pivot_projection.PivotRefused) as excinfo:
        _wide_matrix(
            600,
            {
                "cursor": token,
                "filters": [{"field": "k_day", "in": ["2026-08-01"]}],
            },
        )
    assert excinfo.value.code == "cursor_does_not_describe_this_page"
    # The sentence names the gesture, not the mechanism.
    assert "first page" in excinfo.value.message


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"values": ["m_0"]}, "another value set is another matrix"),
        ({"rows": ["k_day"], "columns": ["k_campaign"]}, "swapped axes are another matrix"),
        ({"row_sort": "desc"}, "another order puts other rows on page 2"),
        ({"subtotals": True}, "totals change what the page carries"),
        ({"grand_total": "both"}, "totals change what the page carries"),
    ],
)
def test_every_shape_the_page_depends_on_is_bound(change, why):
    token = _wide_matrix(600)["bounds"]["next_row_cursor"]
    with pytest.raises(pivot_projection.PivotRefused) as excinfo:
        _wide_matrix(600, {"cursor": token, **change})
    assert excinfo.value.code == "cursor_does_not_describe_this_page", why


def test_a_smaller_budget_is_not_another_matrix():
    """The byte budget chooses how much of a page is served, not which rows.

    Binding it would refuse a caller who resized their window, and refusing what
    is not wrong teaches people to ignore refusals.
    """
    token = _wide_matrix(600)["bounds"]["next_row_cursor"]
    assert pivot_projection.read_cursor(
        token,
        project_id="proj_EXAMPLE",
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        request={**_wide_request(), "max_response_bytes": 60_000},
    )


def test_a_continuation_of_another_result_is_refused():
    token = _wide_matrix(600)["bounds"]["next_row_cursor"]
    with pytest.raises(pivot_projection.PivotRefused) as excinfo:
        pivot_projection.read_cursor(
            token,
            project_id="proj_EXAMPLE",
            result_id="qr_OTHER",
            content_hash="a" * 64,
            request=_wide_request(),
        )
    assert excinfo.value.code == "cursor_does_not_describe_this_page"


def test_a_continuation_of_another_project_is_refused():
    """Same Result id, another Project: the token grants nothing across the line."""
    token = _wide_matrix(600)["bounds"]["next_row_cursor"]
    with pytest.raises(pivot_projection.PivotRefused) as excinfo:
        pivot_projection.read_cursor(
            token,
            project_id="proj_OTHER",
            result_id="qr_EXAMPLE",
            content_hash="a" * 64,
            request=_wide_request(),
        )
    assert excinfo.value.code == "cursor_does_not_describe_this_page"


def test_a_result_that_was_re_executed_refuses_its_old_continuation():
    """The content hash is bound, so page 2 of yesterday's answer is refused."""
    token = _wide_matrix(600)["bounds"]["next_row_cursor"]
    with pytest.raises(pivot_projection.PivotRefused) as excinfo:
        pivot_projection.read_cursor(
            token,
            project_id="proj_EXAMPLE",
            result_id="qr_EXAMPLE",
            content_hash="b" * 64,
            request=_wide_request(),
        )
    assert excinfo.value.code == "cursor_does_not_describe_this_page"


@pytest.mark.parametrize("token", ["", "not-a-cursor", "a.b", 17, None, "x" * 9000])
def test_a_token_that_is_not_ours_is_malformed_not_mismatched(token):
    """Two refusals, because they send a person to two different places."""
    if token is None:
        # `None` means "no cursor supplied": the bare-offset path, not a refusal.
        assert _wide_matrix(3, {"cursor": None})["bounds"]["row_offset"] == 0
        return
    with pytest.raises(pivot_projection.PivotRefused) as excinfo:
        _wide_matrix(3, {"cursor": token})
    assert excinfo.value.code == "malformed_cursor"


def test_a_tampered_signature_never_becomes_a_page():
    token = _wide_matrix(600)["bounds"]["next_row_cursor"]
    body, signature = token.split(".", 1)
    forged = f"{body}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    with pytest.raises(pivot_projection.PivotRefused) as excinfo:
        _wide_matrix(600, {"cursor": forged})
    assert excinfo.value.code == "malformed_cursor"


def test_a_forged_offset_is_refused_rather_than_served():
    """Editing the body without the key must not move a page one row."""
    import base64
    import json as _json

    token = _wide_matrix(600)["bounds"]["next_row_cursor"]
    body, signature = token.split(".", 1)
    raw = body.encode("ascii")
    document = _json.loads(base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4)))
    document["row_offset"] = 0
    forged_body = (
        base64.urlsafe_b64encode(
            _json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(pivot_projection.PivotRefused) as excinfo:
        _wide_matrix(600, {"cursor": f"{forged_body}.{signature}"})
    assert excinfo.value.code == "malformed_cursor"


def test_a_big_filter_does_not_grow_the_continuation(live_postgres):
    """REJECT of 2026-08-22: the server minted tokens it refused itself.

    The bound shape used to be written VERBATIM into the signed body, and a
    filter is `{field, in: [...]}` with no cap on the values inside one — AC 4
    caps 50 FILTERS, not their contents. Measured then: 100 values gave a 5 124
    byte token, 200 gave 9 658, past the 8 192 `read_cursor` accepts. So beyond
    ~170 values every continuation came back `malformed_cursor` — the WRONG one
    of the two refusals, about a token the server had just signed — and the
    console pager stopped working, because once a page is remembered as a token
    it has no fallback.

    The fingerprint binds exactly as hard and never grows.
    """
    sizes = set()
    for count in (1, 100, 500, 5_000):
        token = pivot_projection.mint_cursor(
            project_id="proj_EXAMPLE",
            result_id="qr_EXAMPLE",
            content_hash="a" * 64,
            request=_wide_request(
                {"filters": [{"field": "k_day", "in": [f"v{i}" for i in range(count)]}]}
            ),
            row_offset=3,
            column_offset=0,
        )
        sizes.add(len(token))
        assert len(token) < 8_192, count

    assert len(sizes) == 1, "the token size must not depend on the request at all"


def test_the_fingerprint_still_tells_two_shapes_apart(live_postgres):
    """Hashing the shape must not weaken the binding it replaces."""
    def _token(request):
        return pivot_projection.mint_cursor(
            project_id="proj_EXAMPLE", result_id="qr_EXAMPLE",
            content_hash="a" * 64, request=request, row_offset=3, column_offset=0,
        )

    one = _wide_request({"filters": [{"field": "k_day", "in": ["a"]}]})
    other = _wide_request({"filters": [{"field": "k_day", "in": ["b"]}]})
    assert _token(one) != _token(other)
    with pytest.raises(pivot_projection.PivotRefused) as excinfo:
        pivot_projection.read_cursor(
            _token(one), project_id="proj_EXAMPLE", result_id="qr_EXAMPLE",
            content_hash="a" * 64, request=other,
        )
    assert excinfo.value.code == "cursor_does_not_describe_this_page"


def test_an_explicit_offset_beside_a_cursor_wins_on_its_own_axis(live_postgres):
    """REJECT of 2026-08-22: walking back on one axis reset the other.

    A remembered page is the token of the page that was LEFT, carrying BOTH its
    offsets as they stood then — so after a move on the other axis it is stale by
    construction. Restoring both is the reader losing a column they never asked
    to leave, which is the exact failure the per-axis tokens exist to prevent.

    The offsets are the token's PAYLOAD, not its identity: they are signed but
    never compared, so overriding one changes nothing about what the token
    proves.
    """
    token = pivot_projection.mint_cursor(
        project_id="proj_EXAMPLE",
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        request=_wide_request(),
        row_offset=0,
        column_offset=0,
    )
    matrix = _wide_matrix(600, {"cursor": token, "column_offset": 6})

    assert matrix["bounds"]["row_offset"] == 0
    assert matrix["bounds"]["column_offset"] == 6


def test_the_continuation_is_inside_the_byte_budget_it_travels_in():
    """Minted before `response_bytes` is measured, so the budget counts it."""
    matrix = _wide_matrix(600)
    served = len(json.dumps(matrix, separators=(",", ":"), default=str).encode("utf-8"))
    assert matrix["bounds"]["next_row_cursor"] in json.dumps(matrix, default=str)
    assert served <= pivot_projection.MAX_RESPONSE_BYTES
    assert matrix["bounds"]["response_bytes"] == served


# ---------------------------------------------------------------------------
# Lifecycle: nothing half-written, ever
# ---------------------------------------------------------------------------


def test_a_refused_compilation_stores_nothing(live_postgres):
    org_id, project_id = make_project(live_postgres, "Nothing stored")
    with live_postgres.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.query_spec_versions WHERE project_id = %s",
                    (project_id,))
        before = cur.fetchone()[0]

    with pytest.raises(plans.PlanRefused):
        plans.compile_plan(
            live_postgres,
            project_id=project_id,
            request={"members": [{"datastream_id": "ds_nope"}], "edges": []},
        )

    with live_postgres.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.query_spec_versions WHERE project_id = %s",
                    (project_id,))
        assert cur.fetchone()[0] == before


def test_the_member_cap_is_enforced_and_names_itself(live_postgres):
    _org, project_id = make_project(live_postgres, "Too many")
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=project_id,
            request={
                "members": [{"datastream_id": f"ds_{index}"} for index in range(5)],
                "edges": [],
                "inclusion_policy": "matched_only",
            },
        )
    assert excinfo.value.code == "member_count_out_of_bounds"
    assert "4" in excinfo.value.message
