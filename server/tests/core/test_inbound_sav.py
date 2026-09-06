"""Story 38.12: the SPSS .sav adapter, into the shared tabular envelope.

Every fixture here is a REAL .sav, written by the reader's own writer at test
time. A hand-rolled byte string could not carry a value label, a user-missing
range or a variable label, which are the three things this adapter exists to
handle -- so a described fixture would test the parts that do not matter.

Covers:
  (a) Variables, labels, types and value labels land in the SHARED ParseResult.
  (b) A variable label is EVIDENCE, never a binding (AC3).
  (c) Value labels do NOT replace the coded data (the arithmetic is the point).
  (d) User-missing sentinels become null, and their codes are preserved.
  (e) Dates and strings survive; NaN never leaks as a float.
  (f) Corrupt, non-SPSS and empty files fail with STABLE codes (AC2).
  (g) A missing reader is an environment fact, not a corrupt file.
  (h) The envelope is the same dataclass the CSV parser returns (AC5).
  (i) The scanner recognises .sav by magic bytes, so the gate runs first.
"""

from __future__ import annotations

import datetime as dt

import pytest

pyreadstat = pytest.importorskip(
    "pyreadstat",
    reason="the SPSS reader is a declared runtime dependency; install it to run these",
)
pd = pytest.importorskip("pandas")


# ---------------------------------------------------------------------------
# Real fixtures.
# ---------------------------------------------------------------------------


def _write_sav(tmp_path, frame, **kwargs) -> bytes:
    """Write a genuine .sav and return its bytes."""
    path = tmp_path / "fixture.sav"
    pyreadstat.write_sav(frame, str(path), **kwargs)
    return path.read_bytes()


@pytest.fixture
def survey_sav(tmp_path) -> bytes:
    """A small survey: a scale with value labels, a free-text field, a date.

    `nps` carries 98/99 as the SPSS convention for refused / not applicable --
    the sentinels that must never be averaged into a metric.
    """
    frame = pd.DataFrame(
        {
            "respondent": ["r1", "r2", "r3", "r4"],
            "nps": [9.0, 2.0, 98.0, 99.0],
            "age": [34.0, 51.0, 28.0, 45.0],
            "surveyed_on": [
                dt.datetime(2026, 1, 1),
                dt.datetime(2026, 1, 2),
                dt.datetime(2026, 1, 3),
                dt.datetime(2026, 1, 4),
            ],
        }
    )
    return _write_sav(
        tmp_path,
        frame,
        column_labels=[
            "Respondent identifier",
            "Q3. How likely are you to recommend us?",
            "Age in years",
            "Date surveyed",
        ],
        variable_value_labels={
            "nps": {
                2.0: "Not at all likely",
                9.0: "Extremely likely",
                98.0: "Refused",
                99.0: "Not applicable",
            }
        },
        missing_ranges={"nps": [{"lo": 98.0, "hi": 99.0}]},
    )


# ---------------------------------------------------------------------------
# (a) (h) The shared envelope.
# ---------------------------------------------------------------------------


def test_a_sav_parses_into_the_same_dataclass_as_a_csv(survey_sav):
    """AC5: no second envelope, therefore no second ledger."""
    from core.csv_excel_import import ParseResult
    from core.inbound_sav import parse_sav

    result = parse_sav(survey_sav)

    assert isinstance(result, ParseResult)
    assert [c.name for c in result.columns] == [
        "respondent", "nps", "age", "surveyed_on"
    ]
    assert result.detected_row_count == 4
    assert result.encoding == "sav"
    assert result.delimiter is None
    assert len(result.content_hash) == 64


def test_the_detected_types_use_the_shared_vocabulary(survey_sav):
    """A SAV-specific type name would leak the format into the mapping surface."""
    from core.inbound_sav import parse_sav

    types = {c.name: c.detected_type for c in parse_sav(survey_sav).columns}
    assert types["respondent"] == "text"
    assert types["age"] == "integer"
    assert types["surveyed_on"] == "date"
    assert types["nps"] in ("integer", "decimal")


# ---------------------------------------------------------------------------
# (b) (c) Labels are evidence, not data.
# ---------------------------------------------------------------------------


def test_a_variable_label_is_evidence_not_a_column_name(survey_sav):
    """AC3.

    If the label became the column name, a survey author rewording a question
    would silently re-key a published dataset.
    """
    from core.inbound_sav import parse_sav

    result = parse_sav(survey_sav)

    assert "nps" in [c.name for c in result.columns]
    assert "Q3. How likely are you to recommend us?" not in [
        c.name for c in result.columns
    ]
    # ... and it is not lost either: it rides beside the data for a human.
    assert (
        result.metadata["variable_labels"]["nps"]
        == "Q3. How likely are you to recommend us?"
    )


def test_value_labels_do_not_replace_the_coded_values(survey_sav):
    """Substituting "Extremely likely" for 9 destroys the ordinal arithmetic.

    And it would do it invisibly -- the column would still look like a column.
    """
    from core.inbound_sav import parse_sav

    result = parse_sav(survey_sav)

    answered = [row["nps"] for row in result.rows if row["nps"] is not None]
    assert answered == [9.0, 2.0]
    assert "Extremely likely" not in str(result.rows)
    # The labels are available for display, keyed by code.
    assert result.metadata["value_labels"]["nps"]["9.0"] == "Extremely likely"


# ---------------------------------------------------------------------------
# (d) User-missing.
# ---------------------------------------------------------------------------


def test_user_missing_sentinels_become_null_and_their_codes_survive(survey_sav):
    """The single most expensive way to be wrong about survey data.

    Left as numbers, 98 and 99 are averaged into every metric computed from the
    column: a mean NPS of 52 on a 0-10 scale, and nothing in the pipeline
    objects.
    """
    from core.inbound_sav import parse_sav

    result = parse_sav(survey_sav)
    values = [row["nps"] for row in result.rows]

    assert values[2] is None and values[3] is None
    assert 98.0 not in [v for v in values if v is not None]
    assert 99.0 not in [v for v in values if v is not None]
    # Nulled, not erased: the sentinels are declared.
    assert result.metadata["missing_ranges"]["nps"]
    assert result.metadata["user_missing_applied"] is True
    # And the column's null count reflects them.
    nps_col = next(c for c in result.columns if c.name == "nps")
    assert nps_col.null_count == 2


def test_keeping_the_raw_codes_is_possible_and_declared(survey_sav):
    """Opt-out exists for a mapping that handles the sentinels explicitly."""
    from core.inbound_sav import parse_sav

    result = parse_sav(survey_sav, apply_user_missing=False)
    values = [row["nps"] for row in result.rows]

    assert 98.0 in values and 99.0 in values
    assert result.metadata["user_missing_applied"] is False


# ---------------------------------------------------------------------------
# (e) Value fidelity.
# ---------------------------------------------------------------------------


def test_dates_and_strings_survive_and_are_json_safe(survey_sav):
    import json

    from core.inbound_sav import parse_sav

    result = parse_sav(survey_sav)

    assert result.rows[0]["respondent"] == "r1"
    assert str(result.rows[0]["surveyed_on"]).startswith("2026-01-01")
    # NaN travels through JSON as the literal `NaN`, which is not valid JSON,
    # and it compares unequal to itself. It must never reach here.
    blob = json.dumps(result.rows)
    assert "NaN" not in blob


# ---------------------------------------------------------------------------
# (f) (g) Failures carry stable codes.
# ---------------------------------------------------------------------------


def test_a_non_spss_payload_is_refused_by_signature():
    from core.inbound_sav import SAV_CORRUPT, SavAdapterError, parse_sav

    with pytest.raises(SavAdapterError) as exc:
        parse_sav(b"date,clicks\n2026-01-01,10\n")
    assert exc.value.code == SAV_CORRUPT


def test_a_truncated_sav_is_refused_with_the_same_stable_code(survey_sav):
    """Corrupt metadata must not surface a library stack-shaped message."""
    from core.inbound_sav import SAV_CORRUPT, SavAdapterError, parse_sav

    with pytest.raises(SavAdapterError) as exc:
        parse_sav(survey_sav[: len(survey_sav) // 3])
    assert exc.value.code == SAV_CORRUPT
    assert "Traceback" not in str(exc.value)


def test_a_sav_with_no_rows_is_refused(tmp_path):
    from core.inbound_sav import SAV_EMPTY, SavAdapterError, parse_sav

    empty = _write_sav(tmp_path, pd.DataFrame({"q1": pd.Series([], dtype="float64")}))
    with pytest.raises(SavAdapterError) as exc:
        parse_sav(empty)
    assert exc.value.code == SAV_EMPTY


def test_a_missing_reader_is_an_environment_fact_not_a_corrupt_file(
    survey_sav, monkeypatch
):
    """AC2, and the distinction that saves an operator a wasted request.

    Reporting an uninstalled reader as a corrupt file would send them back to
    the sender for a perfectly good extract.
    """
    import builtins

    from core.inbound_sav import SAV_UNAVAILABLE, SavAdapterError, parse_sav

    real_import = builtins.__import__

    def _no_pyreadstat(name, *args, **kwargs):
        if name == "pyreadstat":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_pyreadstat)

    with pytest.raises(SavAdapterError) as exc:
        parse_sav(survey_sav)
    assert exc.value.code == SAV_UNAVAILABLE


def test_the_temporary_file_does_not_outlive_the_read(survey_sav, tmp_path):
    """The quarantined object is the durable copy; this is a scratch read."""
    import tempfile

    before = set(pathlib_listdir(tempfile.gettempdir()))
    from core.inbound_sav import parse_sav

    parse_sav(survey_sav)
    after = set(pathlib_listdir(tempfile.gettempdir()))
    leaked = [n for n in (after - before) if n.endswith(".sav")]
    assert leaked == [], f"a scratch .sav was left behind: {leaked}"


def pathlib_listdir(path):
    import os

    try:
        return os.listdir(path)
    except OSError:  # pragma: no cover
        return []


# ---------------------------------------------------------------------------
# (i) The scan gate runs before this module ever opens the file.
# ---------------------------------------------------------------------------


def test_the_scanner_recognises_a_real_sav_by_its_magic_bytes(survey_sav):
    from core.inbound_scan import detect_content_type, scan_bytes

    assert detect_content_type(survey_sav) == "application/x-spss-sav"
    verdict = scan_bytes(
        survey_sav,
        declared_type="application/x-spss-sav",
        malware_scanner=lambda data: ("clean", "fixture", "clamav-test"),
    )
    assert verdict.accepted is True
    assert verdict.detected_type == "application/x-spss-sav"


def test_a_sav_declared_as_csv_is_refused_by_the_scanner(survey_sav):
    """The claim/evidence check applies to this format like any other."""
    from core.inbound_scan import REASON_TYPE_MISMATCH, scan_bytes

    assert scan_bytes(
        survey_sav,
        declared_type="text/csv",
        malware_scanner=lambda data: ("clean", "fixture", "clamav-test"),
    ).reason == REASON_TYPE_MISMATCH


def test_looks_like_sav_reads_bytes_not_a_filename(survey_sav):
    from core.inbound_sav import looks_like_sav

    assert looks_like_sav(survey_sav) is True
    assert looks_like_sav(b"not an spss file at all") is False


# ---------------------------------------------------------------------------
# Wiring. A parser the pipeline never reaches is not a supported format.
# ---------------------------------------------------------------------------


def _patch_ingest_preconditions(monkeypatch, ii):
    monkeypatch.setattr(
        ii, "_load_ingestable_datastream", lambda conn, **kw: {
            "source_kind": "managed_feed",
            "enabled": True,
            "config": {"channels": ["email"]},
            "current_plan_version_id": "plan_v1",
            "current_mapping_version_id": "map_v1",
        }
    )
    monkeypatch.setattr(ii, "_fetch_projection_plan", lambda *a, **kw: {})
    monkeypatch.setattr(ii, "_fetch_active_parsing_contract", lambda *a, **kw: {})
    monkeypatch.setattr(ii, "_read_allow_empty_publication", lambda *a, **kw: False)
    # THE LIST IS THE PRECONDITIONS, AND ONE JOINED THE CODE WITHOUT JOINING IT.
    # `ingest_inbound_file` also resolves the pinned plan/mapping bundle, which
    # opens a cursor -- and these two tests hand in a bare `object()` on purpose,
    # because what they prove is a FORMAT decision and no database belongs in it.
    # The result was `AttributeError: 'object' object has no attribute 'cursor'`,
    # raised from a seam neither test is about.
    #
    # The stub carries the bundle's real shape rather than `{}`: a helper that
    # returns something the caller could not have received teaches the next
    # reader the wrong contract.
    monkeypatch.setattr(
        ii,
        "_fetch_mapping_bundle",
        lambda *a, **kw: {
            "mapping_payload": {"fields": [{"field_id": "q1"}]},
            "mapping_fingerprint": "map-fp",
            "source_schema_hash": "schema-hash",
            "capability_fingerprint": "cap-fp",
            "plan_fingerprint": "plan-fp",
            "plan_capability_fingerprint": "plan-cap-fp",
            "plan_contract_version": "1",
        },
    )


def test_unattended_sav_replays_the_confirmed_contract(survey_sav, monkeypatch):
    """Without this, a .sav reaches `detect_format` and dies as "unknown type".

    The operator would read "could not determine file type" about a file whose
    type is unambiguous in its first four bytes.
    """
    import core.csv_excel_import as cei
    import core.inbound_ingest as ii

    captured: dict = {}
    _patch_ingest_preconditions(monkeypatch, ii)
    # THE CONTRACT TRAVELS WRAPPED IN ITS EVIDENCE, not bare. `ingest_inbound_file`
    # freezes `{contract, contract_id, contract_fingerprint}` and refuses a frozen
    # bundle whose `contract` is not a dict -- `InboundContractReviewRequired`,
    # "Frozen parsing contract evidence is incomplete". The stub returned the bare
    # `{"format": "sav"}`, which is what `run_import` receives at the far end but
    # not what this seam hands over.
    monkeypatch.setattr(
        ii,
        "_fetch_active_parsing_contract",
        lambda *a, **kw: {
            "contract": {"format": "sav"},
            "contract_id": "ipc_sav",
            "contract_fingerprint": "sav-fp",
        },
    )
    monkeypatch.setattr(cei, "resolve_file_source_producer", lambda *a, **kw: None)

    def _run_import(data, **kwargs):
        captured["producer"] = kwargs.get("producer")
        captured["contract"] = kwargs.get("contract")
        return {"blocked": False, "ledger": {"id": "mfl_sav"}}

    monkeypatch.setattr(cei, "run_import", _run_import)

    ii.ingest_inbound_file(
        object(),
        datastream_id="ds-1",
        project_id="proj-1",
        file_bytes=survey_sav,
        filename="survey.sav",
        channel="email",
        message_id="evt-1",
        actor="inbound-worker",
    )

    assert captured["producer"] is None
    assert captured["contract"] == {"format": "sav"}


def test_a_declared_template_outranks_the_sav_guess(survey_sav, monkeypatch):
    """An explicit human decision beats a format guess, including a correct one."""
    import core.csv_excel_import as cei
    import core.inbound_ingest as ii

    # THE OBJECT DOES NOT SURVIVE, AND IT IS NOT SUPPOSED TO. This test compared
    # `is sentinel`, and the pipeline now FREEZES the declared producer into a
    # dispatch bundle and rebuilds it on the far side
    # (`_producer_from_frozen_bundle`) -- so identity is gone by design, and a
    # bare `object()` cannot even be frozen: it has no `.template`.
    #
    # What the test's name is about survives untouched: a DECLARED template
    # outranks a format guess. So the producer carries a distinctive template id
    # and that is what is asserted at the far end -- the decision, not the
    # address of the object that carried it.
    from core.file_source_resolution import FileSourceProducer

    declared = FileSourceProducer(
        {"name": "survey template"},
        {"q1": "answer"},
        "fst_DECLARED",
        confirmation_operation_id="op_1",
        confirmation_evidence={"confirmed_by": "owner@example.com"},
    )
    captured: dict = {}
    _patch_ingest_preconditions(monkeypatch, ii)
    monkeypatch.setattr(cei, "resolve_file_source_producer", lambda *a, **kw: declared)

    def _run_import(data, **kwargs):
        captured["producer"] = kwargs.get("producer")
        return {"blocked": False, "ledger": {"id": "mfl_sav"}}

    monkeypatch.setattr(cei, "run_import", _run_import)

    ii.ingest_inbound_file(
        object(),
        datastream_id="ds-1",
        project_id="proj-1",
        file_bytes=survey_sav,
        filename="survey.sav",
        channel="email",
        message_id="evt-2",
        actor="inbound-worker",
    )

    producer = captured["producer"]
    assert producer is not None, "the declared template was dropped for a format guess"
    assert producer.template_id == "fst_DECLARED"
    assert producer.template == {"name": "survey template"}
