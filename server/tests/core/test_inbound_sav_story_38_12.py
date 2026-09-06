"""Review closure conformance for Story 38.12."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

GOLDEN_SAV = base64.b64decode(
    "JEZMM0AoIykgU1BTUyBEQVRBIEZJTEUgLSBodHRwczovL2dpdGh1Yi5jb20vV2l6YXJkTWFjL1JlYWRTdGF0IAIAAAADAAAAAgAAAAAAAAACAAAAAAAAAAAAWUAwMiBBdWcgMjYwMToxOTowMiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAAAAACAAAAAAAAAAEAAAABAAAAAggFAAIIBQBJRCAgICAgIAgAAABJZGVudGl0eQAAAAAAwFhAAgAAAAEAAAABAAAAAAAAAAABAQAAAQEATEFCRUwgICAFAAAATGFiZWwAAAACAAAAAAAAAAEAAAAAAAAAABQWAAAUFgBXSEVOICAgIAQAAABXaGVuAwAAAAIAAAAAAAAAAADwPwNvbmUgICAgAAAAAAAAAEADdHdvICAgIAQAAAABAAAAAQAAAAcAAAADAAAABAAAAAgAAAAUAAAAAAAAAAAAAAD/////AQAAAAEAAAACAAAA6f0AAAcAAAAEAAAACAAAAAMAAAD////////v/////////+9//v//////7/8HAAAACwAAAAQAAAAJAAAAAAAAAAgAAAABAAAAAAAAAAgAAAAAAAAAAAAAAAgAAAABAAAABwAAAA0AAAABAAAAGwAAAElEPWlkCUxBQkVMPWxhYmVsCVdIRU49d2hlbgcAAAAQAAAACAAAAAIAAAABAAAAAAAAAAIAAAAAAAAA5wMAAAAAAABLAgAAAAAAAIgCAAAAAAAAMAAAAAAAAAB4nEv9+5cBBBIVIADItAjl5XJK+/v3D0g8CSF+JB4oDgAXWwqhnP////////8AAAAAAAAAAADwPwABAAAASwIAAAAAAABjAgAAAAAAADAAAAAlAAAA"
)


GOLDEN_MISSING_SAV = base64.b64decode(
    "JEZMMkAoIykgU1BTUyBEQVRBIEZJTEUgLSBodHRwczovL2dpdGh1Yi5jb20vV2l6YXJkTWFjL1JlYWRTdGF0IAIAAAACAAAAAAAAAAAAAAADAAAAAAAAAAAAWUAwMiBBdWcgMjYwMzowMTowNyAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAAAAACAAAAAAAAAAAAAAABAAAAAggFAAIIBQBOVU0gICAgIAAAAAAAgFhAAgAAAAIAAAAAAAAAAQAAAAACAQAAAgEAVEVYVCAgICBOQSAgICAgIAcAAAADAAAABAAAAAgAAAAUAAAAAAAAAAAAAAD/////AQAAAAEAAAACAAAA6f0AAAcAAAAEAAAACAAAAAMAAAD////////v/////////+9//v//////7/8HAAAACwAAAAQAAAAGAAAAAAAAAAgAAAABAAAAAAAAAAgAAAAAAAAABwAAAA0AAAABAAAAEQAAAE5VTT1udW0JVEVYVD10ZXh0BwAAABAAAAAIAAAAAgAAAAEAAAAAAAAAAwAAAAAAAADnAwAAAAAAAAAAAAAAAPA/b2sgICAgICAAAAAAAIBYQE5BICAgICAg////////7/8gICAgICAgIA=="
)

GOLDEN_UNCOMPRESSED_999_DATA = base64.b64decode(
    "JEZMMkAoIykgU1BTUyBEQVRBIEZJTEUgLSBodHRwczovL2dpdGh1Yi5jb20vV2l6YXJkTWFjL1JlYWRTdGF0IAIAAAACAAAAAAAAAAAAAAABAAAAAAAAAAAAWUAwMiBBdWcgMjYwMzowMzoxOCAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAAAAACAAAAAAAAAAAAAAAAAAAAAggFAAIIBQBWQUxVRSAgIAIAAAAAAAAAAAAAAAAAAAACCAUAAggFAE9USEVSICAgBwAAAAMAAAAEAAAACAAAABQAAAAAAAAAAAAAAP////8BAAAAAQAAAAIAAADp/QAABwAAAAQAAAAIAAAAAwAAAP///////+//////////73/+///////v/wcAAAALAAAABAAAAAYAAAAAAAAACAAAAAEAAAAAAAAACAAAAAEAAAAHAAAADQAAAAEAAAAXAAAAVkFMVUU9dmFsdWUJT1RIRVI9b3RoZXIHAAAAEAAAAAgAAAACAAAAAQAAAAAAAAABAAAAAAAAAOcDAAAAAAAA5wMAAAAAAAAAAAAAAADwPw=="
)


def clean_scanner(data):
    return "clean", "golden-fixture", "clamav-test"


def test_golden_sav_uses_common_preview_registry():
    from core.csv_excel_import import FORMAT_SAV, build_preview, detect_format

    assert detect_format("wrong.csv", GOLDEN_SAV) == FORMAT_SAV
    preview = build_preview(
        GOLDEN_SAV,
        filename="golden.sav",
        contract={"format": "sav"},
    )
    assert preview.format == "sav"
    assert preview.producer_format == "sav"
    assert preview.row_count == 2
    assert preview.schema_fingerprint
    assert preview.envelope_version == "tabular-parse-v1"
    assert [column.name for column in preview.columns] == ["id", "label", "when"]


@pytest.mark.parametrize("compression", ["plain", "compressed", "row_compressed"])
def test_scanner_accepts_valid_sav_storage_modes(tmp_path, compression):
    pd = pytest.importorskip("pandas")
    pyreadstat = pytest.importorskip("pyreadstat")
    from core.inbound_scan import scan_bytes

    path = tmp_path / f"{compression}.sav"
    options = (
        {"compress": True}
        if compression == "compressed"
        else {"row_compress": True}
        if compression == "row_compressed"
        else {}
    )
    pyreadstat.write_sav(
        pd.DataFrame({"short": ["a", "b"], "long": ["x" * 300, "y" * 300]}),
        path,
        **options,
    )
    verdict = scan_bytes(
        path.read_bytes(),
        declared_type="application/x-spss-sav",
        malware_scanner=clean_scanner,
    )
    assert verdict.accepted is True
    assert verdict.evidence["spreadsheet_columns"] == 2
    assert verdict.evidence["spreadsheet_rows"] == 2


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"max_rows": 1}, "sav_row_limit_exceeded"),
        ({"max_columns": 2}, "sav_column_limit_exceeded"),
        ({"max_cells": 5}, "sav_cell_limit_exceeded"),
        ({"max_decompressed_bytes": 1}, "sav_decompression_limit_exceeded"),
        ({"max_memory_bytes": 1}, "sav_memory_limit_exceeded"),
    ],
)
def test_sav_budgets_are_typed(kwargs, code):
    from core.inbound_sav import SavAdapterError, parse_sav

    with pytest.raises(SavAdapterError) as failure:
        parse_sav(GOLDEN_SAV, **kwargs)
    assert failure.value.code == code


def test_contract_rejects_sav_csv_or_excel_options():
    from core.csv_excel_import import InvalidImportContract, validate_import_contract

    with pytest.raises(InvalidImportContract):
        validate_import_contract({"format": "sav", "delimiter": ","})
    with pytest.raises(InvalidImportContract):
        validate_import_contract({"format": "sav", "sheet_name": "Sheet1"})


def test_metadata_read_precedes_materialisation(monkeypatch):
    import pyreadstat
    from core.inbound_sav_v2 import _parse_sav_in_process

    calls = []
    real = pyreadstat.read_sav

    def observed(*args, **kwargs):
        calls.append(bool(kwargs.get("metadataonly")))
        return real(*args, **kwargs)

    monkeypatch.setattr(pyreadstat, "read_sav", observed)
    _parse_sav_in_process(GOLDEN_SAV)
    assert calls == [True, False]


def test_sav_error_is_a_common_import_error():
    from core.csv_excel_import import CsvExcelImportError
    from core.inbound_sav import SavAdapterError, parse_sav

    with pytest.raises(SavAdapterError) as failure:
        parse_sav(b"not-sav")
    assert isinstance(failure.value, CsvExcelImportError)
    assert failure.value.code == "sav_unreadable"


def test_schema_fingerprint_and_evidence_are_deterministic():
    from core.inbound_sav import parse_sav

    first = parse_sav(GOLDEN_SAV)
    second = parse_sav(GOLDEN_SAV)
    assert first.schema_fingerprint == second.schema_fingerprint
    assert first.content_hash == second.content_hash
    assert first.metadata["variable_labels"]["id"] == "Identity"
    assert first.metadata["value_labels"]["id"]["1.0"] == "one"
    assert first.metadata["missing_ranges"]["id"]
    assert first.metadata["applied_decisions"]["value_labels_applied"] is False


def test_uncompressed_case_stream_cannot_spoof_the_dictionary_terminator():
    from core.inbound_scan import scan_bytes

    verdict = scan_bytes(
        GOLDEN_UNCOMPRESSED_999_DATA,
        declared_type="application/x-spss-sav",
        malware_scanner=clean_scanner,
    )
    assert verdict.accepted is True
    assert verdict.evidence["spreadsheet_rows"] == 1
    assert verdict.evidence["spreadsheet_columns"] == 2


def test_missing_goldens_preserve_system_user_and_text_semantics():
    from core.inbound_sav import parse_sav

    applied = parse_sav(GOLDEN_MISSING_SAV)
    raw = parse_sav(GOLDEN_MISSING_SAV, apply_user_missing=False)
    assert applied.rows[1] == {"num": None, "text": None}
    assert applied.rows[2]["num"] is None
    assert applied.metadata["missing_evidence"] == {
        "num": {"system": 1, "user": 1},
        "text": {"system": 0, "user": 1},
    }
    assert raw.rows[1] == {"num": 98.0, "text": "NA"}
    assert raw.metadata["missing_ranges"]["text"] == [{"lo": "NA", "hi": "NA"}]


def test_declared_type_is_enforced_not_only_relabelled():
    from core.inbound_sav import SAV_TYPE_MISMATCH, SavAdapterError, parse_sav

    with pytest.raises(SavAdapterError) as failure:
        parse_sav(GOLDEN_SAV, column_types={"label": "integer"})
    assert failure.value.code == SAV_TYPE_MISMATCH


def test_nonfinite_and_unsupported_values_have_typed_codes():
    from core.inbound_sav import SAV_NONFINITE, SAV_UNSUPPORTED_VALUE, SavAdapterError
    from core.inbound_sav_v2 import _safe

    with pytest.raises(SavAdapterError) as nonfinite:
        _safe(float("inf"), 20)
    assert nonfinite.value.code == SAV_NONFINITE
    with pytest.raises(SavAdapterError) as unsupported:
        _safe(object(), 20)
    assert unsupported.value.code == SAV_UNSUPPORTED_VALUE


def test_isolated_worker_is_killed_at_the_hard_deadline(monkeypatch):
    import io

    import core.inbound_sav_v2 as sav

    class HungWorker:
        returncode = None
        killed = False
        stdin = io.BytesIO()
        stdout = io.BytesIO()

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    class HungThread:
        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return True

    worker = HungWorker()
    monkeypatch.setattr(sav.subprocess, "Popen", lambda *args, **kwargs: worker)
    monkeypatch.setattr(sav.threading, "Thread", lambda *args, **kwargs: HungThread())
    with pytest.raises(sav.SavAdapterError) as failure:
        sav.parse_sav(GOLDEN_SAV, max_seconds=0.01)
    assert failure.value.code == sav.SAV_TIMEOUT
    assert worker.killed is True


def test_discovery_and_mapping_retain_the_sav_error_code(monkeypatch):
    import core.inbound_sav as sav
    from core.inbound_discovery import _parse_for_shape
    from core.inbound_mapping_entry import _read_source_columns

    def refuse(*args, **kwargs):
        raise sav.SavAdapterError(sav.SAV_METADATA_LIMIT, "bounded fixture")

    monkeypatch.setattr(sav, "parse_sav", refuse)
    parsed, discovery_code = _parse_for_shape(GOLDEN_SAV, {"filename": "bad.sav"})
    columns, mapping_code = _read_source_columns(GOLDEN_SAV, {"filename": "bad.sav"})
    assert parsed is None and discovery_code == sav.SAV_METADATA_LIMIT
    assert columns is None and mapping_code == sav.SAV_METADATA_LIMIT


def test_sav_uses_existing_ledger_and_never_publishes_directly():
    from core.csv_excel_import import run_import

    ledger = {"id": "mfl_sav", "outcome": "written", "execution_id": "dse_sav"}
    opened = {
        "ledger": ledger,
        "execution": {"id": "dse_sav", "state": "created"},
        "no_op": False,
        "replay": False,
    }
    with (
        patch("core.import_runner.version_contract", return_value="cic_sav"),
        patch("core.managed_feed_ledger.open_import", return_value=opened) as mock_open,
        patch("core.managed_feed_ledger.record_rows", return_value=ledger),
        patch("core.managed_feed_ledger.evaluate_rejection_gate_for_ledger", return_value=None),
        patch("core.managed_feed_ledger.allocate_landing_relation", return_value="raw.sav"),
        patch("core.managed_feed_ledger.mark_outcome"),
        patch("core.import_runner._land_managed_rows", return_value={"rows": 2}),
    ):
        result = run_import(
            GOLDEN_SAV,
            datastream_id="ds_sav",
            project_id="proj_sav",
            plan_version_id="dsp_sav",
            mapping_version_id="dmap_sav",
            projection_plan={"executable": True},
            actor="operator",
            idempotency_key="sav-ledger",
            source_metadata={"filename": "golden.sav"},
            contract={"format": "sav"},
            conn=MagicMock(),
        )
    call = mock_open.call_args.kwargs
    assert call["feed_format"] == "sav"
    evidence = call["source_metadata"]["parser_evidence"]
    assert evidence["producer_format"] == "sav"
    assert evidence["format_evidence"]["variable_labels"]["id"] == "Identity"
    assert result["published"] is False
    assert result["outcome"] == "written_pending_publication"


def test_worker_response_is_size_bounded_before_unpickle():
    import io

    import core.inbound_sav_v2 as sav

    reader = sav._BoundedReader(io.BytesIO(b"oversized"), 3)
    with pytest.raises(sav._ResponseLimitExceeded):
        reader.read()


def test_malformed_worker_payload_has_stable_environment_code(monkeypatch):
    import io
    import pickle

    import core.inbound_sav_v2 as sav

    class MalformedWorker:
        returncode = 0
        stdin = io.BytesIO()
        stdout = io.BytesIO(pickle.dumps(("unknown",)))

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(sav.subprocess, "Popen", lambda *args, **kwargs: MalformedWorker())
    with pytest.raises(sav.SavAdapterError) as failure:
        sav.parse_sav(GOLDEN_SAV)
    assert failure.value.code == sav.SAV_WORKER_ERROR


def test_unix_memory_limit_failure_is_fail_closed(monkeypatch):
    import sys
    import types

    import core.inbound_sav_worker as worker

    fake_resource = types.SimpleNamespace(
        RLIMIT_AS=1,
        RLIM_INFINITY=-1,
        getrlimit=lambda *args: (-1, -1),
        setrlimit=lambda *args: (_ for _ in ()).throw(OSError("denied")),
    )
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    monkeypatch.setattr(
        worker, "os", types.SimpleNamespace(name="posix", sysconf=lambda *args: 4096)
    )
    with pytest.raises(worker.SavAdapterError) as failure:
        worker._limit_memory(128 * 1024 * 1024)
    assert failure.value.code == worker.SAV_WORKER_ERROR


def test_reap_timeout_never_leaks_subprocess_exception():
    import subprocess

    import core.inbound_sav_v2 as sav

    class StuckWorker:
        def kill(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("sav-worker", timeout)

    with pytest.raises(sav.SavAdapterError) as failure:
        sav._reap_worker(StuckWorker())
    assert failure.value.code == sav.SAV_WORKER_ERROR


def test_nonzero_exit_rejects_even_a_complete_ok_payload(monkeypatch):
    import io
    import pickle

    import core.inbound_sav_v2 as sav

    parsed = sav._parse_sav_in_process(GOLDEN_SAV)

    class OomAfterPayloadWorker:
        returncode = -9
        stdin = io.BytesIO()
        stdout = io.BytesIO(pickle.dumps(("ok", parsed)))

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(sav.subprocess, "Popen", lambda *args, **kwargs: OomAfterPayloadWorker())
    with pytest.raises(sav.SavAdapterError) as failure:
        sav.parse_sav(GOLDEN_SAV)
    assert failure.value.code == sav.SAV_MEMORY_LIMIT
