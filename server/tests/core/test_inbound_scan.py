"""Story 38.10: bound and scan a quarantined file before any parser sees it.

The fixtures below are built, not described: a real zip bomb, a real traversal
entry, a real mistyped container. A test that asserts a scanner refuses a
"malicious file" represented by the string "malicious" proves nothing about the
scanner.

Covers:
  (a) Detection reads the BYTES; a declared type can never change the answer.
  (b) Size, both bounds -- and empty is a rejection, not a small file.
  (c) A declared type contradicted by the evidence is refused, not corrected.
  (d) Archive bounds: entry count, compression ratio, uncompressed total,
      traversal in entry names, corrupt container.
  (e) Measuring an archive never decompresses it.
  (f) Null bytes in something decodable as text.
  (g) Column envelope, over a bounded head.
  (h) Malware is three-valued: never "clean" when no scanner exists.
  (i) The stored verdict carries evidence and NO content.
  (j) scan_raw_import walks RECEIVED -> SCANNING -> ACCEPTED|REJECTED and a
      rejection is terminal.
  (k) Untrusted names are bounded and defused for display.
"""

from __future__ import annotations

import io
import struct
import zipfile

import pytest

# ---------------------------------------------------------------------------
# Real fixtures.
# ---------------------------------------------------------------------------


def _zip(entries: dict[str, bytes], *, compress=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compress) as z:
        for name, payload in entries.items():
            z.writestr(name, payload)
    return buf.getvalue()


def _xlsx(sheet: bytes, *, include_relationships: bool = True) -> bytes:
    if b"xmlns=" not in sheet.split(b">", 1)[0]:
        sheet = sheet.replace(
            b"<worksheet",
            b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"',
            1,
        )
    entries = {
        "[Content_Types].xml": (
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
            b'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            b'<Override PartName="/xl/worksheets/sheet1.xml" '
            b'ContentType="application/vnd.openxmlformats-officedocument.'
            b'spreadsheetml.worksheet+xml"/>'
            b"</Types>"
        ),
        "xl/workbook.xml": (
            b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            b'<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/worksheets/sheet1.xml": sheet,
    }
    if include_relationships:
        entries.update(
            {
                "_rels/.rels": (
                    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                    b'officeDocument/2006/relationships/officeDocument" '
                    b'Target="xl/workbook.xml"/>'
                    b"</Relationships>"
                ),
                "xl/_rels/workbook.xml.rels": (
                    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                    b'officeDocument/2006/relationships/worksheet" '
                    b'Target="worksheets/sheet1.xml"/>'
                    b"</Relationships>"
                ),
            }
        )
    return _zip(entries)


def _sav(*, columns: int, rows: int) -> bytes:
    header = bytearray(b"$FL2" + b"pytest structural SAV".ljust(60, b" "))
    header.extend(b"\x00" * (176 - len(header)))
    struct.pack_into("<i", header, 64, 2)
    struct.pack_into("<i", header, 68, columns)
    struct.pack_into("<i", header, 72, 0)
    struct.pack_into("<i", header, 80, rows)
    dictionary = bytearray()
    for index in range(columns):
        record = bytearray(32)
        struct.pack_into("<iiiiii", record, 0, 2, 0, 0, 0, 5, 5)
        record[24:32] = f"V{index + 1}".encode().ljust(8, b" ")
        dictionary.extend(record)
    dictionary.extend(struct.pack("<ii", 999, 0))
    case_data = b"\x00" * (max(rows, 0) * columns * 8)
    return bytes(header + dictionary + case_data)


def _zip_bomb(*, entry_bytes: int = 8 * 1024 * 1024) -> bytes:
    """A real, small archive that expands enormously.

    Eight megabytes of a single repeated byte deflate to a few kilobytes -- a
    compression ratio in the thousands. Nothing here is simulated.
    """
    return _zip({"bomb.csv": b"0" * entry_bytes})


class _CleanScanner:
    version = "test-clean-scanner-v1"

    def __call__(self, data: bytes) -> tuple[str, str]:
        return "clean", "test-signature-db"


@pytest.fixture(autouse=True)
def _configured_clean_scanner(monkeypatch):
    from core import inbound_scan

    monkeypatch.setattr(
        inbound_scan,
        "configured_malware_scanner",
        lambda **kwargs: _CleanScanner(),
    )


_CSV = b"date,clicks,cost\n2026-01-01,10,5.00\n2026-01-02,12,6.00\n"


# ---------------------------------------------------------------------------
# (a) Detection is evidence-driven.
# ---------------------------------------------------------------------------


def test_detection_reads_the_bytes_not_the_claim():
    from core.inbound_scan import detect_content_type

    archive = _zip({"a.csv": _CSV})
    # The most confident possible lie about a container.
    assert detect_content_type(archive, declared_type="text/csv") == "application/zip"
    assert detect_content_type(_CSV, declared_type="application/zip") == "text/csv"


def test_detection_refuses_to_guess_from_a_weak_signal():
    from core.inbound_scan import detect_content_type

    assert detect_content_type(b"\x00\x01\x02\x03binary") == "application/octet-stream"
    assert detect_content_type(b"") == "application/octet-stream"


def test_detection_recognises_the_sav_magic_for_story_38_12():
    from core.inbound_scan import detect_content_type

    assert detect_content_type(b"$FL2" + b"\x00" * 60) == "application/x-spss-sav"


# ---------------------------------------------------------------------------
# (b) Size.
# ---------------------------------------------------------------------------


def test_empty_is_a_rejection_not_a_small_file():
    from core.inbound_scan import REASON_EMPTY, scan_bytes

    v = scan_bytes(b"")
    assert v.accepted is False
    assert v.reason == REASON_EMPTY


def test_oversized_is_refused_before_anything_is_inspected():
    from core.inbound_scan import REASON_TOO_LARGE, scan_bytes

    v = scan_bytes(_CSV * 100, max_bytes=64)
    assert v.accepted is False
    assert v.reason == REASON_TOO_LARGE
    assert v.evidence["size_limit"] == 64


def test_a_valid_csv_is_accepted():
    from core.inbound_scan import scan_bytes

    v = scan_bytes(_CSV, declared_type="text/csv")
    assert v.accepted is True
    assert v.reason is None
    assert v.detected_type == "text/csv"


# ---------------------------------------------------------------------------
# (c) The claim versus the evidence.
# ---------------------------------------------------------------------------


def test_a_container_declared_as_text_is_refused_not_corrected():
    """This is the shape of the attack: get a parser to open a zip as a csv."""
    from core.inbound_scan import REASON_TYPE_MISMATCH, scan_bytes

    v = scan_bytes(_zip({"a.csv": _CSV}), declared_type="text/csv")
    assert v.accepted is False
    assert v.reason == REASON_TYPE_MISMATCH
    # Both sides are preserved. Neither overwrites the other.
    assert v.declared_type == "text/csv"
    assert v.detected_type == "application/zip"


@pytest.mark.parametrize("declared", [None, "", "application/octet-stream", "binary/octet-stream"])
def test_an_unspecific_claim_is_not_a_contradiction(declared):
    """Most senders declare nothing useful; refusing them would refuse traffic."""
    from core.inbound_scan import scan_bytes

    assert scan_bytes(_CSV, declared_type=declared).accepted is True


def test_an_unsupported_type_is_refused_by_name():
    from core.inbound_scan import REASON_UNSUPPORTED_TYPE, scan_bytes

    v = scan_bytes(b"%PDF-1.7\n%rest of a pdf")
    assert v.accepted is False
    assert v.reason == REASON_UNSUPPORTED_TYPE
    assert v.detected_type == "application/pdf"


# ---------------------------------------------------------------------------
# (d) (e) Archive bounds, against real archives.
# ---------------------------------------------------------------------------


def test_a_real_zip_bomb_is_refused_on_its_ratio():
    from core.inbound_scan import (
        REASON_ARCHIVE_RATIO,
        REASON_ARCHIVE_TOO_LARGE,
        scan_bytes,
    )

    bomb = _zip_bomb()
    # It really is small on the wire and enormous expanded.
    assert len(bomb) < 100_000

    v = scan_bytes(bomb, max_uncompressed_bytes=1_000_000)
    assert v.accepted is False
    assert v.reason in (REASON_ARCHIVE_RATIO, REASON_ARCHIVE_TOO_LARGE)
    assert v.evidence["archive_uncompressed_bytes"] == 8 * 1024 * 1024


def test_the_ratio_bound_fires_even_when_the_total_would_fit():
    """Two independent bounds, and the ratio one must not be shadowed."""
    from core.inbound_scan import REASON_ARCHIVE_RATIO, scan_bytes

    v = scan_bytes(_zip_bomb(), max_uncompressed_bytes=10**9, max_compression_ratio=50)
    assert v.reason == REASON_ARCHIVE_RATIO
    assert v.evidence["archive_worst_ratio"] > 50


def test_too_many_entries_is_refused():
    from core.inbound_scan import REASON_ARCHIVE_TOO_MANY_ENTRIES, scan_bytes

    many = _zip({f"f{i}.csv": b"a,b\n1,2\n" for i in range(40)})
    v = scan_bytes(many, max_archive_entries=10)
    assert v.reason == REASON_ARCHIVE_TOO_MANY_ENTRIES
    assert v.evidence["archive_entries"] == 40


def test_a_traversal_entry_name_is_refused():
    from core.inbound_scan import REASON_ARCHIVE_TRAVERSAL, scan_bytes

    v = scan_bytes(_zip({"../../etc/passwd": b"root:x:0:0\n"}))
    assert v.accepted is False
    assert v.reason == REASON_ARCHIVE_TRAVERSAL


def test_an_absolute_entry_name_is_refused():
    from core.inbound_scan import REASON_ARCHIVE_TRAVERSAL, scan_bytes

    assert scan_bytes(_zip({"/etc/shadow": b"x"})).reason == REASON_ARCHIVE_TRAVERSAL


def test_a_corrupt_container_is_refused_not_crashed_on():
    from core.inbound_scan import REASON_ARCHIVE_CORRUPT, scan_bytes

    v = scan_bytes(b"PK\x03\x04" + b"\xff" * 200)
    assert v.accepted is False
    assert v.reason == REASON_ARCHIVE_CORRUPT
    assert v.evidence["archive_readable"] is False


def test_measuring_an_archive_never_decompresses_it(monkeypatch):
    """The bound must be readable from the directory alone.

    A scanner that extracts in order to measure IS the vector it exists to
    stop. Patching ZipFile.read to explode proves nothing calls it.
    """
    from core.inbound_scan import scan_bytes

    # Build the fixtures FIRST: `ZipFile.open` is also how an archive is
    # WRITTEN, so patching it before this point would break the fixture rather
    # than test the scanner.
    bomb = _zip_bomb()
    benign = _zip({"a.csv": _CSV})

    def _explode(*a, **kw):
        raise AssertionError("the scanner decompressed an entry")

    monkeypatch.setattr(zipfile.ZipFile, "read", _explode)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", _explode)
    monkeypatch.setattr(zipfile.ZipFile, "open", _explode)

    scan_bytes(bomb, max_uncompressed_bytes=1000)
    scan_bytes(benign)


def test_a_generic_zip_is_not_offered_to_the_xlsx_parser():
    from core.inbound_scan import REASON_ARCHIVE_NOT_SPREADSHEET, scan_bytes

    v = scan_bytes(_zip({"report.csv": _CSV}))
    assert v.accepted is False
    assert v.reason == REASON_ARCHIVE_NOT_SPREADSHEET
    assert v.evidence["archive_entries"] == 1


# ---------------------------------------------------------------------------
# (f) (g) Text checks.
# ---------------------------------------------------------------------------


def test_null_bytes_are_refused():
    from core.inbound_scan import REASON_NULL_BYTES, detect_content_type, scan_bytes

    # Decodes as text, but carries a NUL: a binary wearing a text extension.
    payload = b"date,clicks\n2026-01-01,\x00\x00\n"
    if detect_content_type(payload) == "text/csv":
        assert scan_bytes(payload).reason == REASON_NULL_BYTES
    else:
        # Detection already classified it as binary -- also a refusal, by type.
        assert scan_bytes(payload).accepted is False


def test_column_envelope_is_bounded():
    from core.inbound_scan import REASON_TOO_MANY_COLUMNS, scan_bytes

    wide = (b",".join(b"c%d" % i for i in range(5000))) + b"\n1,2,3\n"
    v = scan_bytes(wide, max_columns=100)
    assert v.reason == REASON_TOO_MANY_COLUMNS
    assert v.evidence["column_estimate"] >= 5000


def test_a_normal_width_file_passes_the_envelope():
    from core.inbound_scan import scan_bytes

    v = scan_bytes(_CSV)
    assert v.accepted is True
    assert v.evidence["column_estimate"] == 3


# ---------------------------------------------------------------------------
# (h) Malware honesty.
# ---------------------------------------------------------------------------


def test_no_scanner_accepts_a_parseable_file_but_never_calls_it_clean(monkeypatch):
    """Sans antivirus, un tableau passe — et le verdict dit qu'aucune signature
    n'a été cherchée.

    La règle précédente — pas de scanner, pas d'entrée — a été renversée par Jean
    le 2026-08-05, et ce test tient exactement ce qui a changé et ce qui n'a pas
    changé. Ce qui protège est la liste blanche de formats que ce fichier a déjà
    franchie : type réellement détecté, aucun désaccord avec le type déclaré,
    aucun octet nul, archive inspectée, `_PARSEABLE`, bornes de lignes et de
    colonnes. Un tableau qu'on parse en lignes n'exécute rien.

    Ce qui reste interdit, et c'est l'objet de la section « malware honesty » :
    présenter une absence de vérification comme une vérification. Le verdict
    porte donc `not_run`, un moteur `none` et la politique nommée — jamais
    `clean`.
    """
    from core import inbound_scan

    def unavailable(**kwargs):
        raise inbound_scan.ScanRetryableError(inbound_scan.REASON_MALWARE_UNAVAILABLE)

    monkeypatch.setattr(inbound_scan, "configured_malware_scanner", unavailable)
    verdict = inbound_scan.scan_bytes(_CSV)
    assert verdict.accepted is True
    assert verdict.malware == "not_run"
    assert verdict.malware != "clean"
    assert verdict.malware_engine == "none"
    assert verdict.evidence["malware_policy"] == inbound_scan.MALWARE_POLICY_FORMAT_ALLOWLIST


def test_no_scanner_does_not_rescue_a_file_the_format_gate_refuses(monkeypatch):
    """La liste blanche reste la barrière : ce qu'elle refuse reste refusé.

    Sans cette preuve, la règle nouvelle se lirait « sans antivirus, tout
    passe ». Un exécutable porte des octets nuls et n'est pas parseable : il est
    rejeté avant même que la question de l'antivirus se pose.
    """
    from core import inbound_scan

    def unavailable(**kwargs):
        raise inbound_scan.ScanRetryableError(inbound_scan.REASON_MALWARE_UNAVAILABLE)

    monkeypatch.setattr(inbound_scan, "configured_malware_scanner", unavailable)
    verdict = inbound_scan.scan_bytes(b"MZ\x90\x00\x03\x00\x00\x00binary payload" * 8)
    assert verdict.accepted is False
    assert verdict.malware == "not_run"


def test_an_infected_verdict_refuses_the_file():
    from core.inbound_scan import scan_bytes

    v = scan_bytes(_CSV, malware_scanner=lambda data: ("infected", "test-signature"))
    assert v.accepted is False
    assert v.reason == "malware_detected"
    assert v.malware == "infected"


def test_a_broken_scanner_is_not_a_clean_file():
    from core.inbound_scan import ScanRetryableError, scan_bytes

    def _broken(data):
        raise RuntimeError("scanner down")

    with pytest.raises(ScanRetryableError) as failure:
        scan_bytes(_CSV, malware_scanner=_broken)
    assert failure.value.code == "malware_scanner_unavailable"


def test_a_clean_scanner_result_is_carried_through():
    from core.inbound_scan import scan_bytes

    v = scan_bytes(_CSV, malware_scanner=lambda data: ("clean", "sig-db-2026-07"))
    assert v.accepted is True
    assert v.malware == "clean"


# ---------------------------------------------------------------------------
# (i) The stored verdict discloses evidence, never content.
# ---------------------------------------------------------------------------


def test_the_stored_verdict_carries_no_file_content():
    from core.inbound_scan import scan_bytes

    secret = b"date,email\n2026-01-01,someone@example.com\n"
    blob = str(scan_bytes(secret, declared_type="text/csv").as_scan_verdict())
    assert "someone@example.com" not in blob
    assert "2026-01-01" not in blob


def test_the_stored_verdict_is_json_safe():
    import json

    from core.inbound_scan import scan_bytes

    for payload in (_CSV, _zip({"a.csv": _CSV}), b"", b"%PDF-1.7"):
        json.dumps(scan_bytes(payload).as_scan_verdict())


# ---------------------------------------------------------------------------
# (k) Untrusted display.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["=cmd|'/c calc'!A1", "+1+1", "-2+3", "@SUM(A1)"])
def test_a_formula_leader_is_defused(raw):
    from core.inbound_scan import neutralise_untrusted

    assert neutralise_untrusted(raw).startswith("'")


def test_untrusted_names_are_bounded_and_stripped_of_control_characters():
    from core.inbound_scan import neutralise_untrusted

    out = neutralise_untrusted("a\x00b\x1bc" + "x" * 500)
    assert "\x00" not in out and "\x1b" not in out
    assert len(out) <= 120


def test_neutralise_passes_none_through():
    from core.inbound_scan import neutralise_untrusted

    assert neutralise_untrusted(None) is None


# ---------------------------------------------------------------------------
# (j) The governed walk.
# ---------------------------------------------------------------------------


def test_scan_raw_import_walks_scanning_then_accepted(monkeypatch):
    from core import inbound_scan

    calls: list[dict] = []

    def _mark(conn, *, raw_import_id, datastream_id, state, **kw):
        calls.append({"state": state, **kw})
        return {"state": state}

    import core.inbound_raw_imports as iri

    monkeypatch.setattr(iri, "mark_raw_import_state", _mark)

    verdict = inbound_scan.scan_raw_import(
        object(),
        raw_import_id="inbraw_1",
        datastream_id="ds-1",
        data=_CSV,
        declared_type="text/csv",
    )

    assert verdict.accepted is True
    assert [c["state"] for c in calls] == ["SCANNING", "ACCEPTED"]
    # SCANNING is written BEFORE the work, so a worker that dies mid-scan
    # leaves a row saying what it was doing.
    final = calls[-1]
    assert final["media_type_detected"] == "text/csv"
    assert final["scan_verdict"]["accepted"] is True
    assert final["error_code"] is None


def test_scan_raw_import_records_a_terminal_rejection_without_raising(monkeypatch):
    """A refused file must not abort its siblings -- so it is a verdict, not a raise."""
    from core import inbound_scan

    calls: list[dict] = []

    def _mark(conn, *, raw_import_id, datastream_id, state, **kw):
        calls.append({"state": state, **kw})
        return {"state": state}

    import core.inbound_raw_imports as iri

    monkeypatch.setattr(iri, "mark_raw_import_state", _mark)

    verdict = inbound_scan.scan_raw_import(
        object(),
        raw_import_id="inbraw_2",
        datastream_id="ds-1",
        data=_zip_bomb(),
        declared_type="application/zip",
        max_uncompressed_bytes=1000,
    )

    assert verdict.accepted is False
    assert [c["state"] for c in calls] == ["SCANNING", "REJECTED"]
    assert calls[-1]["error_code"] == verdict.reason
    assert calls[-1]["scan_verdict"]["accepted"] is False


# ---------------------------------------------------------------------------
# Closure adversarial matrix (review wave 2026-08-01).
# ---------------------------------------------------------------------------


def test_text_row_envelope_counts_logical_rows():
    from core.inbound_scan import REASON_TOO_MANY_ROWS, scan_bytes

    verdict = scan_bytes(b"a,b\n1,2\n3,4\n", max_rows=2)
    assert verdict.reason == REASON_TOO_MANY_ROWS
    assert verdict.evidence["row_estimate"] == 3


def test_quoted_newline_header_cannot_hide_columns():
    from core.inbound_scan import REASON_TOO_MANY_COLUMNS, scan_bytes

    payload = b'"first\ncontinued",second,third\n1,2,3\n'
    verdict = scan_bytes(payload, max_columns=2)
    assert verdict.reason == REASON_TOO_MANY_COLUMNS
    assert verdict.evidence["column_estimate"] == 3


def test_unterminated_quoted_header_is_bounded_and_rejected():
    from core.inbound_scan import REASON_HEADER_TOO_LARGE, scan_bytes

    payload = b'"' + b"a" * (64 * 1024) + b"\n1,2\n"
    verdict = scan_bytes(payload)
    assert verdict.reason == REASON_HEADER_TOO_LARGE
    assert verdict.evidence["header_terminated"] is False


def test_whitespace_only_payload_is_rejected():
    from core.inbound_scan import REASON_WHITESPACE_ONLY, scan_bytes

    assert scan_bytes(b" \t\r\n  \n").reason == REASON_WHITESPACE_ONLY


def test_utf16_tabular_payload_is_supported_conservatively():
    from core.inbound_scan import scan_bytes

    payload = "date,clicks\n2026-01-01,2\n".encode("utf-16")
    verdict = scan_bytes(payload, declared_type="text/csv; charset=utf-16")
    assert verdict.accepted is True
    assert verdict.evidence["encoding"] in {"utf-16-le", "utf-16-be"}


def test_control_bytes_after_64k_are_not_classified_as_csv():
    from core.inbound_scan import scan_bytes

    payload = b"a,b\n" + b"1,2\n" * 17000 + b"\x01tail"
    verdict = scan_bytes(payload, declared_type="text/csv")
    assert verdict.accepted is False


@pytest.mark.parametrize(
    "claim",
    ["application/pdf", "application/zip", "application/vnd.ms-excel", "image/png"],
)
def test_positive_mime_contradictions_are_exhaustively_rejected(claim):
    from core.inbound_scan import REASON_TYPE_MISMATCH, scan_bytes

    assert scan_bytes(_CSV, declared_type=claim).reason == REASON_TYPE_MISMATCH


def test_xlsx_dimensions_are_observed_from_cells_not_dimension_claim():
    from core.inbound_scan import REASON_TOO_MANY_COLUMNS, scan_bytes

    sheet = (
        b'<worksheet><dimension ref="A1"/><sheetData>'
        b'<row r="1"><c r="A1"/><c r="C1"/></row>'
        b'<row r="2"><c r="A2"/></row></sheetData></worksheet>'
    )
    verdict = scan_bytes(_xlsx(sheet), max_columns=2)
    assert verdict.reason == REASON_TOO_MANY_COLUMNS
    assert verdict.evidence["spreadsheet_columns"] == 3


def test_real_structural_xlsx_is_accepted_with_observed_dimensions():
    from core.inbound_scan import scan_bytes

    sheet = b'<worksheet><sheetData><row r="1"><c r="A1"/></row></sheetData></worksheet>'
    verdict = scan_bytes(_xlsx(sheet))
    assert verdict.accepted is True
    assert verdict.detected_type.endswith("spreadsheetml.sheet")
    assert verdict.evidence["spreadsheet_rows"] == 1


def test_xlsx_row_envelope_counts_physical_rows_even_when_refs_repeat():
    from core.inbound_scan import REASON_TOO_MANY_ROWS, scan_bytes

    rows = b"".join(b'<row r="1"><c r="A1"/></row>' for _ in range(4))
    sheet = b"<worksheet><sheetData>" + rows + b"</sheetData></worksheet>"
    verdict = scan_bytes(_xlsx(sheet), max_rows=3)

    assert verdict.reason == REASON_TOO_MANY_ROWS
    assert verdict.evidence["spreadsheet_rows"] == 4


def test_xlsx_named_zip_without_relationship_structure_is_rejected():
    from core.inbound_scan import REASON_ARCHIVE_NOT_SPREADSHEET, scan_bytes

    sheet = b'<worksheet><sheetData><row r="1"/></sheetData></worksheet>'
    assert (
        scan_bytes(_xlsx(sheet, include_relationships=False)).reason
        == REASON_ARCHIVE_NOT_SPREADSHEET
    )


def test_sav_unknown_case_count_is_not_treated_as_zero():
    from core.inbound_scan import REASON_DIMENSIONS_UNKNOWN, scan_bytes

    assert scan_bytes(_sav(columns=3, rows=-1)).reason == REASON_DIMENSIONS_UNKNOWN


def test_sav_observed_bounds_are_enforced():
    from core.inbound_scan import REASON_TOO_MANY_ROWS, scan_bytes

    verdict = scan_bytes(_sav(columns=3, rows=12), max_rows=10)
    assert verdict.reason == REASON_TOO_MANY_ROWS
    assert verdict.evidence["spreadsheet_rows"] == 12


def test_encrypted_zip_entry_is_rejected_before_xml_read():
    from core.inbound_scan import REASON_ARCHIVE_ENCRYPTED, scan_bytes

    payload = bytearray(_xlsx(b"<worksheet/>"))
    local = payload.find(b"PK\x03\x04")
    central = payload.find(b"PK\x01\x02")
    struct.pack_into("<H", payload, local + 6, struct.unpack_from("<H", payload, local + 6)[0] | 1)
    struct.pack_into(
        "<H", payload, central + 8, struct.unpack_from("<H", payload, central + 8)[0] | 1
    )
    assert scan_bytes(bytes(payload)).reason == REASON_ARCHIVE_ENCRYPTED


def test_zero_compressed_size_with_content_is_infinite_ratio():
    from core.inbound_scan import REASON_ARCHIVE_RATIO, scan_bytes

    payload = bytearray(_xlsx(b"<worksheet/>"))
    central = payload.find(b"PK\x01\x02")
    struct.pack_into("<I", payload, central + 20, 0)
    assert scan_bytes(bytes(payload)).reason == REASON_ARCHIVE_RATIO


def test_eocd_entry_count_lie_is_rejected_before_infolist(monkeypatch):
    from core.inbound_scan import REASON_ARCHIVE_CORRUPT, scan_bytes

    payload = bytearray(_zip({"a": b"1", "b": b"2"}))
    eocd = payload.rfind(b"PK\x05\x06")
    struct.pack_into("<H", payload, eocd + 10, 1)

    def explode(*args, **kwargs):
        raise AssertionError("infolist must not run after failed central preflight")

    monkeypatch.setattr(zipfile.ZipFile, "infolist", explode)
    assert scan_bytes(bytes(payload)).reason == REASON_ARCHIVE_CORRUPT


def test_time_and_memory_budgets_are_typed_retryable_outcomes():
    from core.inbound_scan import (
        REASON_SCAN_MEMORY,
        REASON_SCAN_TIMEOUT,
        ScanRetryableError,
        scan_bytes,
    )

    with pytest.raises(ScanRetryableError) as timeout:
        scan_bytes(_CSV, max_scan_seconds=-1)
    assert timeout.value.code == REASON_SCAN_TIMEOUT
    with pytest.raises(ScanRetryableError) as memory:
        scan_bytes(_CSV, max_memory_bytes=1)
    assert memory.value.code == REASON_SCAN_MEMORY


def test_malformed_clean_claim_is_not_an_explicit_clean_verdict():
    from core.inbound_scan import REASON_MALWARE_INVALID, ScanRetryableError, scan_bytes

    with pytest.raises(ScanRetryableError) as failure:
        scan_bytes(_CSV, malware_scanner=lambda data: "clean")
    assert failure.value.code == REASON_MALWARE_INVALID


def test_versioned_engine_and_neutral_detail_are_the_only_scanner_strings_stored():
    from core.inbound_scan import scan_bytes

    verdict = scan_bytes(
        _CSV, malware_scanner=lambda data: ("clean", "=hostile prompt", "clamav-1.4.3-db-27888")
    )
    assert verdict.malware_engine == "clamav-1.4.3-db-27888"
    assert verdict.malware_detail == "scanner_clean"
    assert "hostile" not in str(verdict.as_scan_verdict())


def test_scanning_intent_commits_before_retryable_scanner_work(monkeypatch):
    from core import inbound_scan

    events: list[str] = []

    class Conn:
        def commit(self):
            events.append("commit")

    def mark(conn, *, state, **kwargs):
        events.append(state)
        return {"state": state}

    def unavailable(data):
        events.append("scanner")
        raise inbound_scan.ScanRetryableError(inbound_scan.REASON_MALWARE_UNAVAILABLE)

    import core.inbound_raw_imports as raw_imports

    monkeypatch.setattr(raw_imports, "mark_raw_import_state", mark)
    with pytest.raises(inbound_scan.ScanRetryableError):
        inbound_scan.scan_raw_import(
            Conn(),
            raw_import_id="inbraw_durable",
            datastream_id="ds-1",
            data=_CSV,
            malware_scanner=unavailable,
            persist_intent=True,
        )
    assert events == ["SCANNING", "commit", "scanner"]


def test_worksheet_validation_never_materializes_an_element_tree(monkeypatch):
    import xml.etree.ElementTree as element_tree

    from core.inbound_scan import scan_bytes

    original_parse = element_tree.parse
    parsed_names = []

    def bounded_metadata_parse(source, *args, **kwargs):
        name = getattr(source, "name", "")
        parsed_names.append(name)
        if name.startswith("xl/worksheets/"):
            raise AssertionError("worksheet must use the streaming parser")
        return original_parse(source, *args, **kwargs)

    monkeypatch.setattr(element_tree, "parse", bounded_metadata_parse)
    sheet = b'<worksheet><sheetData><row r="1"><c r="A1"/></row></sheetData></worksheet>'
    assert scan_bytes(_xlsx(sheet)).accepted is True
    assert all(not name.startswith("xl/worksheets/") for name in parsed_names)


def test_xlsx_giant_attribute_is_rejected_before_expat_can_buffer_it():
    from core.inbound_scan import REASON_ARCHIVE_NOT_SPREADSHEET, scan_bytes

    giant = b"a" * 5000
    sheet = b'<worksheet hostile="' + giant + b'"><sheetData/></worksheet>'
    verdict = scan_bytes(_xlsx(sheet), max_memory_bytes=64 * 1024)
    assert verdict.reason == REASON_ARCHIVE_NOT_SPREADSHEET

def test_xlsx_excessive_xml_depth_is_rejected_by_streaming_envelope():
    from core.inbound_scan import REASON_ARCHIVE_NOT_SPREADSHEET, scan_bytes

    nested = b"<x>" * 65 + b"</x>" * 65
    sheet = b"<worksheet><sheetData>" + nested + b"</sheetData></worksheet>"
    assert scan_bytes(_xlsx(sheet)).reason == REASON_ARCHIVE_NOT_SPREADSHEET


def test_empty_ooxml_placeholders_are_not_a_spreadsheet():
    from core.inbound_scan import REASON_ARCHIVE_NOT_SPREADSHEET, scan_bytes

    payload = _zip(
        {
            "[Content_Types].xml": b"<Types/>",
            "_rels/.rels": b"<Relationships/>",
            "xl/workbook.xml": b"<workbook/>",
            "xl/_rels/workbook.xml.rels": b"<Relationships/>",
            "xl/worksheets/sheet1.xml": b"<worksheet/>",
        }
    )
    assert scan_bytes(payload).reason == REASON_ARCHIVE_NOT_SPREADSHEET


def test_truncated_sav_header_is_rejected_before_the_parser():
    from core.inbound_scan import REASON_DIMENSIONS_UNKNOWN, scan_bytes

    truncated = bytearray(b"$FL2" + b" " * 84)
    struct.pack_into("<i", truncated, 64, 2)
    struct.pack_into("<i", truncated, 68, 3)
    struct.pack_into("<i", truncated, 80, 12)
    assert scan_bytes(bytes(truncated)).reason == REASON_DIMENSIONS_UNKNOWN


def test_fake_ooxml_namespaces_and_relationship_uris_are_rejected():
    from core.inbound_scan import REASON_ARCHIVE_NOT_SPREADSHEET, scan_bytes

    sheet = b'<worksheet><sheetData><row r="1"><c r="A1"/></row></sheetData></worksheet>'
    with zipfile.ZipFile(io.BytesIO(_xlsx(sheet))) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    replacements = {
        b"http://schemas.openxmlformats.org/package/2006/content-types": b"urn:evil-content",
        b"http://schemas.openxmlformats.org/package/2006/relationships": b"urn:evil-package",
        b"http://schemas.openxmlformats.org/spreadsheetml/2006/main": b"urn:evil-sheet",
        b"http://schemas.openxmlformats.org/officeDocument/2006/relationships": b"urn:evil-office",
    }
    for name, value in entries.items():
        for expected, fake in replacements.items():
            value = value.replace(expected, fake)
        entries[name] = value
    assert scan_bytes(_zip(entries)).reason == REASON_ARCHIVE_NOT_SPREADSHEET


def test_compressed_sav_without_a_case_stream_is_rejected():
    from core.inbound_scan import REASON_DIMENSIONS_UNKNOWN, scan_bytes

    truncated = bytearray(_sav(columns=1, rows=1))
    struct.pack_into("<i", truncated, 72, 1)
    del truncated[-8:]
    assert scan_bytes(bytes(truncated)).reason == REASON_DIMENSIONS_UNKNOWN
