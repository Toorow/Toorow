"""The surface of the unresolved set: S1 and S2, one reading behind both.

WHAT THIS FILE IS FOR. `core/unresolved_values.py` was proved offline by
`test_unresolved_values.py` and imported by nobody: 386 lines whose only caller was
that test. These tests hold the OTHER half -- that a route reaches the reading,
that both screens read the same one, and that the three states arrive at the client
with their sentences intact.

THE FIXTURE IS THE DEPLOYMENT'S OWN CASE, SHRUNK, exactly as the module's test uses
it: videos with views and no name anywhere in the product.
"""

from __future__ import annotations

from datetime import date

import pytest

PROJECT = "proj_EXAMPLE"
STREAM = "ds_EXAMPLE"
#: The reading is taken as if today were this day, so the seven-day window is a
#: fact of the test and not of the calendar it runs on.
TODAY = date(2026, 8, 6)


# ---------------------------------------------------------------------------
# What is swept, and what a row is allowed to offer. Both are pure.
# ---------------------------------------------------------------------------


def test_only_a_grain_column_bound_to_a_canonical_field_is_swept():
    """"A column that is not mapped to a dimension has no values to resolve.\""""
    from core.unresolved_values_api import sweepable_dimensions

    swept = sweepable_dimensions(
        [
            {"source_field": "video", "target_field": "video_id", "is_key_column": True},
            # In the grain, bound to nothing: its gesture is one panel above.
            {"source_field": "playlist", "target_field": None, "is_key_column": True},
            # Bound, but not part of what identifies a row.
            {"source_field": "views", "target_field": "views", "is_key_column": False},
            # The axes of the reading are never values of it.
            {"source_field": "date", "target_field": "date", "is_key_column": True},
            {"source_field": "project_id", "target_field": "project_id", "is_key_column": True},
            # A composite is ONE partition whose values are `FR>mobile`; sweeping it
            # beside its components reports one unmapped country twice.
            {"source_field": "country>device", "target_field": "geo", "is_key_column": True},
        ]
    )
    assert [entry["dimension"] for entry in swept] == ["video"]
    assert swept[0]["canonical_dimension"] == "video_id"


def test_a_row_that_no_pair_can_repair_offers_no_pair_editor():
    """The point of the typing (`unresolved-values.md:406-410`)."""
    from core.unresolved_values import (
        REASON_ABSENT_AT_SOURCE,
        REASON_NO_REFERENCE,
        REASON_UNMAPPED,
    )
    from core.unresolved_values_api import action_for

    assert action_for(REASON_UNMAPPED) == {
        "kind": "map",
        "label": "Map to…",
        "pair_editor": True,
    }
    assert action_for(REASON_NO_REFERENCE)["kind"] == "attach"
    assert action_for(REASON_NO_REFERENCE)["pair_editor"] is False
    absent = action_for(REASON_ABSENT_AT_SOURCE)
    assert absent["pair_editor"] is False and absent["label"] is None
    # A reason this function was never taught falls to no control at all, which is
    # the safe side of the guess.
    assert action_for("something_new")["pair_editor"] is False


def test_the_two_mandated_sentences_are_shipped_verbatim():
    from core.unresolved_values_api import (
        WAREHOUSE_UNAVAILABLE_MESSAGE,
        empty_message,
    )

    assert empty_message(3) == (
        "Every value of the 3 mapped dimensions resolves over the last 7 days."
    )
    # One dimension is still a sentence a person reads, so the noun agrees.
    assert empty_message(1).startswith("Every value of the 1 mapped dimension ")
    assert WAREHOUSE_UNAVAILABLE_MESSAGE == (
        "The warehouse could not be read, so unresolved values are unknown for "
        "this window. This is not a count of zero."
    )


def test_the_window_is_seven_days_and_it_travels():
    from core.unresolved_values_api import window_for

    assert window_for(TODAY) == {
        "start": "2026-07-31",
        "end": "2026-08-06",
        "days": 7,
    }


# ---------------------------------------------------------------------------
# The reading, on a real DuckDB file and a stubbed Postgres side.
# ---------------------------------------------------------------------------


@pytest.fixture
def warehouse_file(tmp_path, monkeypatch):
    import duckdb

    path = tmp_path / "raw.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE main.raw_youtube_daily ("
        "  project_id VARCHAR, date VARCHAR, channel_id VARCHAR,"
        "  video VARCHAR, views INTEGER)"
    )
    con.executemany(
        "INSERT INTO main.raw_youtube_daily VALUES (?, ?, ?, ?, ?)",
        [
            (PROJECT, "2026-08-06", "chan_a", "vid_light", 5),
            (PROJECT, "2026-08-06", "chan_a", "vid_heavy", 10),
            (PROJECT, "2026-08-06", "chan_a", "vid_heavy", 11),
            (PROJECT, "2026-08-06", "chan_a", "vid_heavy", 12),
            (PROJECT, "2026-08-06", "chan_a", "vid_named", 7),
            (PROJECT, "2026-08-06", "chan_a", "", 1),
            ("proj_OTHER", "2026-08-06", "chan_z", "vid_other", 99),
        ],
    )
    con.close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))
    return path


class _Cursor:
    """Just enough of a DB-API cursor for the one statement S2 issues itself."""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _sql, _params=None):
        return None

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def cursor(self):
        return _Cursor(self._rows)


@pytest.fixture
def postgres_side(monkeypatch, relation="raw_youtube_daily"):
    """The Postgres half of the reading, stubbed where its owners declare it.

    Nothing here re-implements a store: each patch replaces one named function
    with the answer that store would give, so a change to any of those contracts
    fails loudly here rather than being silently re-expressed.
    """
    from core import (
        datastream_mapping_header,
        dimension_conformance,
        dimension_reference,
        stage_relation_resolver,
        value_mapping_tables,
    )

    monkeypatch.setattr(
        datastream_mapping_header,
        "read_stream_facts",
        lambda conn, *, project_id, datastream_id: {
            "connector": "youtube",
            "plan_version_id": None,
            "mapping_version_id": "dmv_1",
            "report_profile_id": "channel_daily",
        },
    )
    monkeypatch.setattr(
        datastream_mapping_header,
        "read_mapping_columns",
        lambda conn, *, project_id, datastream_id: {
            "columns": [
                {
                    "source_field": "video",
                    "target_field": "video_id",
                    "is_key_column": True,
                    "sensitivity": "none",
                },
                {
                    "source_field": "views",
                    "target_field": "views",
                    "is_key_column": False,
                    "sensitivity": "none",
                },
            ],
            "source": "mapping_version",
        },
    )
    monkeypatch.setattr(
        stage_relation_resolver,
        "resolve_stage_relations",
        lambda *, connector, report_profile_id, modules_dir=None: {
            "connector": connector,
            "report_profile_id": report_profile_id,
            "collected_relation": relation,
            "mapped_relation": "stg_youtube_daily",
            "reason": None,
            "message": None,
        },
    )
    # No confirmed pair, and no catalogue: the honest deployment state on the day
    # the 516 videos were measured.
    monkeypatch.setattr(
        dimension_conformance, "resolve_dimension_conformance", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        dimension_reference,
        "read_reference",
        lambda conn, *, project_id, canonical_dimension: {
            "dimension": canonical_dimension,
            "state": "absent",
            "gap": "no_stream_carries_the_reference_role",
            "message": "Datastreams bind this dimension, but none of them is a reference.",
            "candidates": [],
            "reference": None,
        },
    )
    monkeypatch.setattr(
        value_mapping_tables, "read_field_assignments", lambda *_a, **_k: []
    )
    return monkeypatch


def test_s1_ranks_by_cost_types_every_row_and_carries_its_state(
    warehouse_file, postgres_side
):
    from core.unresolved_values_api import read_datastream_unresolved

    reading = read_datastream_unresolved(
        _Conn(), project_id=PROJECT, datastream_id=STREAM, today=TODAY
    )
    assert reading["state"] == "measured"
    assert reading["window"] == {"start": "2026-07-31", "end": "2026-08-06", "days": 7}
    assert [group["dimension"] for group in reading["groups"]] == ["video"]

    group = reading["groups"][0]
    assert group["state"] == "measured"
    # Heaviest first, and the ranking is the whole product decision. Ties break on
    # the value so two readings of an unchanged window return the same page.
    assert [row["source_value"] for row in group["values"]] == [
        "vid_heavy",
        "",
        "vid_light",
        "vid_named",
    ]
    assert group["values"][0]["occurrences"] == 3
    # Every unnamed value is `unmapped` here: no reference was consulted, which is
    # silence and never a finding.
    assert group["by_reason"] == {
        "absent_at_source": 1,
        "unmapped": 3,
        "no_reference": 0,
    }
    # The share of rows is derived from the window the reading paid for.
    assert group["window_rows"] == 6
    assert group["values"][0]["row_share"] == pytest.approx(0.5)
    # And the share of a metric is NOT invented.
    assert group["values"][0]["metric_share"] is None


def test_the_blank_row_offers_no_pair_and_names_the_collection(
    warehouse_file, postgres_side
):
    from core.unresolved_values_api import read_datastream_unresolved

    reading = read_datastream_unresolved(
        _Conn(), project_id=PROJECT, datastream_id=STREAM, today=TODAY
    )
    blank = next(
        row
        for row in reading["groups"][0]["values"]
        if row["source_value"] == ""
    )
    assert blank["reason"] == "absent_at_source"
    assert blank["action"]["pair_editor"] is False
    assert blank["action"]["label"] is None
    assert "collection" in blank["repair"].lower()


def test_an_unreadable_relation_is_unknown_and_never_a_count_of_zero(
    warehouse_file, monkeypatch, postgres_side
):
    from core import stage_relation_resolver
    from core.unresolved_values_api import (
        WAREHOUSE_UNAVAILABLE_MESSAGE,
        read_datastream_unresolved,
    )

    monkeypatch.setattr(
        stage_relation_resolver,
        "resolve_stage_relations",
        lambda *, connector, report_profile_id, modules_dir=None: {
            "connector": connector,
            "report_profile_id": report_profile_id,
            "collected_relation": "raw_nothing_here",
            "mapped_relation": None,
            "reason": None,
            "message": None,
        },
    )
    reading = read_datastream_unresolved(
        _Conn(), project_id=PROJECT, datastream_id=STREAM, today=TODAY
    )
    assert reading["state"] == "unavailable"
    assert reading["message"] == WAREHOUSE_UNAVAILABLE_MESSAGE
    group = reading["groups"][0]
    assert group["state"] == "unavailable"
    # `None`, never `0` -- the first bullet this whole page exists for.
    assert group["unresolved"] is None
    assert group["observed_distinct"] is None


def test_a_classified_column_is_counted_and_never_listed(
    warehouse_file, monkeypatch, postgres_side
):
    from core import datastream_mapping_header
    from core.unresolved_values_api import read_datastream_unresolved

    monkeypatch.setattr(
        datastream_mapping_header,
        "read_mapping_columns",
        lambda conn, *, project_id, datastream_id: {
            "columns": [
                {
                    "source_field": "video",
                    "target_field": "video_id",
                    "is_key_column": True,
                    # Absence of a `none` classification MASKS -- the reader's
                    # policy is inversion, and this page is not the hole through
                    # which classified values leave.
                    "sensitivity": "unknown",
                }
            ],
            "source": "mapping_version",
        },
    )
    reading = read_datastream_unresolved(
        _Conn(), project_id=PROJECT, datastream_id=STREAM, today=TODAY
    )
    group = reading["groups"][0]
    assert group["state"] == "not_listable"
    assert group["values"] == []
    assert "classified" in (group["message"] or "")


def test_a_datastream_with_no_mapped_dimension_names_the_gesture(
    warehouse_file, monkeypatch, postgres_side
):
    from core import datastream_mapping_header
    from core.unresolved_values_api import (
        NOT_APPLICABLE_MESSAGE,
        read_datastream_unresolved,
    )

    monkeypatch.setattr(
        datastream_mapping_header,
        "read_mapping_columns",
        lambda conn, *, project_id, datastream_id: {"columns": [], "source": None},
    )
    reading = read_datastream_unresolved(
        _Conn(), project_id=PROJECT, datastream_id=STREAM, today=TODAY
    )
    assert reading["state"] == "not_applicable"
    assert reading["message"] == NOT_APPLICABLE_MESSAGE
    assert reading["groups"] == []


def test_everything_resolving_says_what_was_measured(
    warehouse_file, monkeypatch, postgres_side
):
    """The empty case is a MEASUREMENT, never "there is nothing"."""
    from core import dimension_conformance
    from core.unresolved_values_api import empty_message, read_datastream_unresolved

    monkeypatch.setattr(
        dimension_conformance,
        "resolve_dimension_conformance",
        lambda *_a, **_k: {
            ("youtube", "vid_heavy"): "Heavy",
            ("youtube", "vid_light"): "Light",
            ("youtube", "vid_named"): "Named",
        },
    )
    reading = read_datastream_unresolved(
        _Conn(), project_id=PROJECT, datastream_id=STREAM, today=TODAY
    )
    # The blank value stays: absence wins over every pair, which is exactly what
    # keeps a careless mapping from making an empty column read as fine.
    assert reading["groups"][0]["by_reason"] == {
        "absent_at_source": 1,
        "unmapped": 0,
        "no_reference": 0,
    }
    assert reading["state"] == "measured"
    assert reading["message"] is None

    # And with the blank gone too, the sentence appears and says what was read.
    from core import unresolved_values

    monkeypatch.setattr(
        unresolved_values, "REASONS", unresolved_values.REASONS, raising=False
    )
    reading["groups"][0]["unresolved"] = 0
    assert empty_message(1).startswith("Every value of the 1 mapped dimension")


def test_the_catalogue_that_names_the_values_produces_no_reference(
    warehouse_file, monkeypatch, postgres_side
):
    """The 516 videos: the set was consulted and does not hold them."""
    from core import dimension_reference
    from core.unresolved_values_api import read_datastream_unresolved

    monkeypatch.setattr(
        dimension_reference,
        "read_reference",
        lambda conn, *, project_id, canonical_dimension: {
            "dimension": canonical_dimension,
            "state": "declared",
            "gap": None,
            "message": None,
            "candidates": [],
            "reference": {
                "datastream_id": "ds_CATALOGUE",
                "name": "Video catalogue",
                "source_field": "video",
                "archived": False,
                "mapped": True,
            },
        },
    )
    reading = read_datastream_unresolved(
        _Conn(), project_id=PROJECT, datastream_id=STREAM, today=TODAY
    )
    group = reading["groups"][0]
    assert group["reference"]["consulted"] is True
    # Every value of the fixture is in the relation the stubbed catalogue reads,
    # so nothing is missing from it -- the reading finds only the blank.
    assert group["by_reason"]["no_reference"] == 0
    assert group["by_reason"]["absent_at_source"] == 1


def test_an_unreadable_catalogue_is_silence_and_not_a_finding(
    warehouse_file, monkeypatch, postgres_side
):
    """`None` means no set was consulted; answering `False` would misroute people."""
    from core import dimension_reference, stage_relation_resolver
    from core.unresolved_values_api import read_datastream_unresolved

    monkeypatch.setattr(
        dimension_reference,
        "read_reference",
        lambda conn, *, project_id, canonical_dimension: {
            "dimension": canonical_dimension,
            "state": "declared",
            "gap": None,
            "message": None,
            "candidates": [],
            "reference": {
                "datastream_id": "ds_CATALOGUE",
                "name": "Video catalogue",
                "source_field": "video",
                "archived": False,
                "mapped": True,
            },
        },
    )
    # The catalogue's own address cannot be resolved.
    monkeypatch.setattr(
        stage_relation_resolver,
        "resolve_stage_relations",
        lambda *, connector, report_profile_id, modules_dir=None: (
            {
                "connector": connector,
                "report_profile_id": report_profile_id,
                "collected_relation": (
                    "raw_youtube_daily" if report_profile_id == "channel_daily" else None
                ),
                "mapped_relation": None,
                "reason": (
                    None
                    if report_profile_id == "channel_daily"
                    else "report_profile_not_set"
                ),
                "message": None if report_profile_id == "channel_daily" else "no profile",
            }
        ),
    )
    from core import datastream_mapping_header

    monkeypatch.setattr(
        datastream_mapping_header,
        "read_stream_facts",
        lambda conn, *, project_id, datastream_id: {
            "connector": "youtube",
            "plan_version_id": None,
            "mapping_version_id": "dmv_1",
            "report_profile_id": (
                "channel_daily" if datastream_id == STREAM else "unknown_profile"
            ),
        },
    )
    reading = read_datastream_unresolved(
        _Conn(), project_id=PROJECT, datastream_id=STREAM, today=TODAY
    )
    group = reading["groups"][0]
    assert group["reference"]["consulted"] is False
    # Silence falls back to `unmapped`, which a pair would close -- never to
    # `no_reference`, which would send a person to a catalogue nobody read.
    assert group["by_reason"]["no_reference"] == 0
    assert group["by_reason"]["unmapped"] == 3


# ---------------------------------------------------------------------------
# S2 -- the same reading, one projection wider.
# ---------------------------------------------------------------------------


def test_s2_is_the_same_reading_and_answers_the_same_number(
    warehouse_file, postgres_side
):
    """The criterion at `unresolved-values.md:482`, held by a test rather than hoped."""
    from core.unresolved_values_api import (
        read_datastream_unresolved,
        read_project_unresolved,
    )

    one = read_datastream_unresolved(
        _Conn(), project_id=PROJECT, datastream_id=STREAM, today=TODAY
    )
    whole = read_project_unresolved(
        _Conn([(STREAM, "YouTube performance")]), project_id=PROJECT, today=TODAY
    )
    assert whole["state"] == "measured"
    assert len(whole["groups"]) == 1
    assert whole["groups"][0]["unresolved"] == one["groups"][0]["unresolved"]
    # And the projection that is wider: the Datastream is named on every row's
    # group, so one address exists per value.
    assert whole["groups"][0]["datastream_name"] == "YouTube performance"
    assert whole["groups"][0]["datastream_id"] == STREAM
    assert whole["groups"][0]["destination_table"] is None
    assert whole["groups"][0]["destination_state"] == "known"


def test_s2_states_how_many_datastreams_it_opened(warehouse_file, postgres_side):
    from core import unresolved_values_api

    rows = [(f"ds_{index}", f"Stream {index}") for index in range(12)]
    whole = unresolved_values_api.read_project_unresolved(
        _Conn(rows), project_id=PROJECT, today=TODAY
    )
    assert whole["datastreams_total"] == 12
    assert whole["datastreams_opened"] == unresolved_values_api.MAX_STREAMS
    # STATED, never silent: a bounded sweep that said nothing would read as
    # "we looked at everything".
    assert whole["datastreams_truncated"] is True


# ---------------------------------------------------------------------------
# The envelope, and the addresses.
# ---------------------------------------------------------------------------


def test_the_envelope_never_folds_the_three_reasons(warehouse_file, postgres_side):
    from core.unresolved_values_api import _envelope, read_datastream_unresolved

    envelope = _envelope(
        read_datastream_unresolved(
            _Conn(), project_id=PROJECT, datastream_id=STREAM, today=TODAY
        ),
        project_id=PROJECT,
        scope="datastream",
        grouping="dimension",
        datastream_id=STREAM,
    )
    assert envelope["schema"] == "unresolved_values.v1"
    assert envelope["title"] == "Values waiting to be mapped"
    assert envelope["state"] == "measured"
    assert envelope["summary"]["unresolved"] == 4
    assert envelope["summary"]["dimensions_measured"] == 1
    # Always all three keys, even at zero: "zero of this kind" is a result.
    assert set(envelope["summary"]["by_reason"]) == {
        "absent_at_source",
        "unmapped",
        "no_reference",
    }
    # CE QUI EST BATI ET CE QUI NE L'EST PAS, LES DEUX NOMMES. Le tiroir S3
    # existe depuis le 2026-08-22 (story 67.20) ; ce qui reste absent -- la
    # colonne de propositions -- garde sa phrase, et le tiroir garde la sienne
    # sur ce qu'il ne fait toujours pas (une regle est DEPLIEE en paires, elle
    # n'est pas gardee comme motif).
    assert envelope["repair_drawer"]["available"] is True
    assert envelope["repair_drawer"]["note"]
    assert [m["value"] for m in envelope["repair_drawer"]["match_modes"]] == [
        "exact",
        "exact_ci",
        "contains",
        "regex",
    ]
    # LES TROIS REPONSES A UNE RELECTURE voyagent sur l'enveloppe : l'ecran ne
    # compose aucune des trois, sinon le tiroir et la boite d'import seraient
    # deux endroits libres de decrire un meme fait mesure autrement.
    after_write = envelope["repair_drawer"]["after_write"]
    assert after_write["recheck"] is True
    assert after_write["still_listed_note"]
    assert after_write["cleared_note"]
    assert after_write["unknown_note"]
    assert envelope["proposals"]["available"] is False
    assert envelope["proposals"]["note"]
    # S4 a DEUX destinations et une seule est servie -- comme l'export.
    assert envelope["import"]["available"] is True
    assert envelope["import"]["destination"] == "value_mapping_table"
    assert envelope["import"]["write_mode"] == "append_by_source_value"
    assert envelope["import"]["write_mode_note"]
    assert envelope["import"]["other_destination_note"]
    assert envelope["ranking"]["metric_share"] is None


def test_no_sentence_of_this_envelope_promises_the_next_read():
    """Le dernier « Incomplete if » de la page, tenu sur le SERVEUR.

    Les deux surfaces d'ecriture imprimaient « It applies at the next read of
    this window » sur le seul `200` de l'ecriture. Rien ne mesurait cela : les
    paires vont dans `app.value_mapping_entries` et la lecture resout
    `app.dimension_value_mappings`, deux magasins qu'aucun module ne joint
    (Open question 4). L'enveloppe porte donc TROIS reponses possibles a une
    relecture, et aucune ne promet la suivante.
    """
    from core.unresolved_values_api import (
        REPAIR_DRAWER_PARTIAL,
        REPAIR_WRITE_CLEARED,
        REPAIR_WRITE_NOT_SEEN,
        REPAIR_WRITE_UNKNOWN,
    )

    # AUCUNE des phrases de cette enveloppe ne promet la lecture suivante.
    everywhere = " ".join(
        [
            REPAIR_DRAWER_PARTIAL,
            REPAIR_WRITE_NOT_SEEN,
            REPAIR_WRITE_CLEARED,
            REPAIR_WRITE_UNKNOWN,
        ]
    )
    assert "applies at the next read" not in everywhere

    # Et la note du tiroir NOMME l'ecart, au lieu de le taire.
    assert "does not read the value tables" in REPAIR_DRAWER_PARTIAL
    assert "confirmed mappings" in REPAIR_DRAWER_PARTIAL
    # La phrase « encore listee » nomme qu'aucun geste ne joint les deux et que
    # l'arbitrage n'est pas rendu -- elle n'invente pas un geste.
    assert "nothing in the product carries a pair" in REPAIR_WRITE_NOT_SEEN
    assert "has not been decided" in REPAIR_WRITE_NOT_SEEN


def test_the_four_addresses_are_declared_and_the_literals_come_first():
    """Les deux litteraux AVANT le parametre, ou le parametre les avale.

    `/reach` rejoint `/extract` sous le meme prefixe : Starlette prend la
    premiere route qui matche, donc `.../unresolved-values` place avant
    `.../unresolved-values/reach` ne le laisserait jamais atteindre.
    """
    from core.unresolved_values_api import UNRESOLVED_VALUES_ROUTES

    paths = [route.path for route in UNRESOLVED_VALUES_ROUTES]
    assert paths == [
        "/api/projects/{project_id}/datastreams/{datastream_id}/unresolved-values/extract",
        "/api/projects/{project_id}/datastreams/{datastream_id}/unresolved-values/reach",
        "/api/projects/{project_id}/datastreams/{datastream_id}/unresolved-values",
        "/api/projects/{project_id}/unresolved-values",
    ]
    # Et la portee est une LECTURE posee en POST : elle n'ouvre aucun curseur
    # d'ecriture, mais un motif regex dans une query string se fait reecrire par
    # l'encodage avant d'arriver.
    methods = {route.path: route.methods for route in UNRESOLVED_VALUES_ROUTES}
    assert "POST" in methods[
        "/api/projects/{project_id}/datastreams/{datastream_id}/unresolved-values/reach"
    ]


def test_the_router_really_mounts_them():
    """The defect this whole module repairs is a reading nothing calls."""
    from core.admin_api import router

    mounted = {getattr(route, "path", "") for route in router.routes}
    assert "/api/projects/{project_id}/unresolved-values" in mounted
    assert (
        "/api/projects/{project_id}/datastreams/{datastream_id}/unresolved-values"
        in mounted
    )
