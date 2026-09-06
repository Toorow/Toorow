"""Story 38.10 -- Bound and scan a quarantined file before any parser sees it.

A parser is an attack surface. This module runs between "the bytes are durably
stored" (38.9) and "the bytes reach a parser" (38.11/38.12), and its whole job is
to decide, on evidence, whether a file may cross that line.

THE SHAPE OF THE ANSWER MATTERS AS MUCH AS THE ANSWER. ``scan_bytes`` is pure and
returns a ``ScanVerdict``: it never touches a database, never raises for a
rejection, and never says "clean" -- it says what it checked, what it found, and
what it could NOT check. The difference is the point. E38-NFR02 asks for malware
scanning "when available", and no scanner is wired into this deployment today; a
verdict that quietly omitted that would read, to every downstream surface, as a
file that passed a malware check. It reports ``malware="unavailable"`` instead,
and that value is carried into the stored evidence.

WHAT IS CHECKED, and why each one is here:

  * SIZE, both bounds. Zero bytes is not a small file, it is an absent one, and
    it must not become an empty publication. The upper bound is enforced again
    here even though the intake already bounds the request body: the intake
    bounds a REQUEST, this bounds an OBJECT, and a reprocessed or re-uploaded
    object never passed through that request.
  * DECLARED VERSUS DETECTED TYPE. The sender's ``Content-Type`` is a claim. The
    magic bytes are evidence. A mismatch is not corrected -- correcting it is
    how a .zip becomes a "text/csv" that a parser opens -- it is REPORTED, and
    the caller decides by policy. Only the detected type may gate the parser.
  * ARCHIVE BOUNDS. A ZIP container (which is also what an XLSX is) is opened
    only as a directory listing, never extracted here. Entry count, per-entry
    compression ratio, total uncompressed size and path traversal in entry names
    are all bounded. A 42 KB file that expands to 4 GB is the classic way to
    turn a parser into an outage.
  * NULL BYTES in something declared as text: the single cheapest signal that a
    binary is wearing a text extension.
  * ROW/COLUMN ENVELOPE, bounded by reading a HEAD of the file only. A file with
    a million columns is not a spreadsheet, and discovering that inside the
    parser is discovering it too late.

WHAT IS DELIBERATELY *NOT* HERE:

  * No parsing. Not one row is interpreted. The envelope check counts separators
    in a bounded prefix; it does not build a table.
  * No decision about duplicates, mappings, or publication. Those belong to the
    Datastream's policy and to Epic 12.
  * No provider vocabulary (AD-2), and no filename ever reaches a path, a
    command, or a prompt. Untrusted strings are returned inside data fields, and
    ``describe_verdict_for_display`` bounds and neutralises them for the one
    place they are shown.

ASCII-only source (AI-03). The caller owns the transaction in ``scan_raw_import``.
"""

from __future__ import annotations

import codecs
import csv
import io
import logging
import os
import posixpath
import re
import socket
import struct
import time
import unicodedata
import xml.etree.ElementTree as etree
import zipfile
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounds. Every one of these is a POLICY DEFAULT, overridable per call, not a
# law of nature -- but a default that refuses is safer than a default that lets
# a parser find out.
# ---------------------------------------------------------------------------

#: Largest object that may reach a parser. Matches the intake body bound
#: (INBOUND_MAX_BODY_BYTES, 25 MiB) so a file cannot pass one gate and fail the
#: other for reasons an operator cannot see.
DEFAULT_MAX_BYTES = 25 * 1024 * 1024

#: Largest total size an archive may expand to. Independent of the compressed
#: bound above: that is what arrives, this is what a parser would hold.
DEFAULT_MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024

#: Largest compression ratio tolerated for any single archive entry.
DEFAULT_MAX_COMPRESSION_RATIO = 200.0

#: Largest number of entries an archive may contain.
DEFAULT_MAX_ARCHIVE_ENTRIES = 512

#: Largest number of columns tolerated in the header line of a tabular file.
DEFAULT_MAX_COLUMNS = 2048


#: Largest logical row count accepted before a format adapter is invoked.
DEFAULT_MAX_ROWS = 1_000_000

#: Hard wall-clock and working-set policy budgets.
DEFAULT_MAX_SCAN_SECONDS = 30.0
DEFAULT_MAX_MEMORY_BYTES = 256 * 1024 * 1024

SCAN_POLICY_VERSION = "inbound-scan-policy-v2"
DEFAULT_MALWARE_ENGINE = "clamav-instream-v1"
#: How many bytes of the head are inspected for the envelope check. Bounded so a
#: 25 MiB file costs the same as a 4 KiB one.
_ENVELOPE_HEAD_BYTES = 64 * 1024

#: Magic prefixes. Only what we can assert; anything else stays ``unknown``
#: rather than being guessed into a type.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),  # empty archive
    (b"PK\x07\x08", "application/zip"),  # spanned archive
    (b"\x1f\x8b", "application/gzip"),
    (b"BZh", "application/x-bzip2"),
    (b"\xfd7zXZ\x00", "application/x-xz"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (b"Rar!\x1a\x07", "application/vnd.rar"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/vnd.ms-excel"),  # legacy OLE
    (b"%PDF-", "application/pdf"),
    (b"$FL2", "application/x-spss-sav"),  # SPSS .sav -- Story 38.12
    (b"$FL3", "application/x-spss-sav"),
)

#: Container types that must be inspected as archives before anything else.
_ZIP_LIKE = frozenset({"application/zip"})

#: Terminal, actionable rejection codes. Stable strings: they reach an operator
#: and an MCP client, so renaming one is a contract change.
REASON_EMPTY = "empty_file"
REASON_TOO_LARGE = "file_too_large"
REASON_TYPE_MISMATCH = "declared_type_mismatch"
REASON_UNSUPPORTED_TYPE = "unsupported_type"
REASON_ARCHIVE_TOO_MANY_ENTRIES = "archive_too_many_entries"
REASON_ARCHIVE_RATIO = "archive_compression_ratio_exceeded"
REASON_ARCHIVE_TOO_LARGE = "archive_uncompressed_too_large"
REASON_ARCHIVE_TRAVERSAL = "archive_entry_path_traversal"
REASON_ARCHIVE_CORRUPT = "archive_unreadable"
REASON_NULL_BYTES = "binary_content_in_text_file"
REASON_TOO_MANY_COLUMNS = "column_envelope_exceeded"

#: Types a tabular parser can be offered. Anything else is refused BY NAME
REASON_TOO_MANY_ROWS = "row_envelope_exceeded"
REASON_HEADER_TOO_LARGE = "header_envelope_exceeded"
REASON_WHITESPACE_ONLY = "whitespace_only_file"
REASON_ARCHIVE_ENCRYPTED = "archive_encrypted"
REASON_ARCHIVE_NOT_SPREADSHEET = "archive_not_supported_spreadsheet"
REASON_SCAN_TIMEOUT = "scan_time_budget_exceeded"
REASON_SCAN_MEMORY = "scan_memory_budget_exceeded"
REASON_ARCHIVE_CENTRAL_DIRECTORY = "archive_central_directory_exceeded"
REASON_MALWARE_UNAVAILABLE = "malware_scanner_unavailable"

#: La politique inscrite au verdict quand aucun scanner n'est configuré : le
#: fichier est accepté parce qu'il a franchi la liste blanche de formats, et le
#: verdict le DIT — `malware: "not_run"`, moteur `none`. Une lecture ne peut donc
#: jamais confondre « aucune signature trouvée » et « aucune signature cherchée ».
MALWARE_POLICY_FORMAT_ALLOWLIST = "format_allowlist_no_scanner"
REASON_MALWARE_INVALID = "malware_scanner_invalid_verdict"
REASON_CONTAINER_UNSUPPORTED = "container_not_supported"
#: rather than handed over and allowed to fail inside the parser.
REASON_DIMENSIONS_UNKNOWN = "spreadsheet_dimensions_unknown"
_PARSEABLE = frozenset(
    {
        "text/csv",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/x-spss-sav",  # Story 38.12
    }
)


# ---------------------------------------------------------------------------


class ScanRetryableError(RuntimeError):
    """A transient/indeterminate scan outcome that must be retried, never parsed."""

    def __init__(self, code: str, *, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.evidence = evidence or {}


# Verdict.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanVerdict:
    """The evidence, and the decision it supports.

    ``accepted`` is the decision. Everything else is why -- and is meant to be
    readable by an operator who was not there.

    ``malware`` is a THREE-valued field on purpose: "clean", "infected", or
    "unavailable". A boolean would have forced a scanner that does not exist
    into looking like one that found nothing.
    """

    accepted: bool
    reason: str | None
    detected_type: str
    declared_type: str | None
    size_bytes: int
    malware_engine: str = DEFAULT_MALWARE_ENGINE
    policy: dict[str, Any] = field(default_factory=dict)
    malware: str = "unavailable"
    malware_detail: str | None = None
    #: Bounded, non-sensitive observations. Never a row, never a cell value.
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_scan_verdict(self) -> dict[str, Any]:
        """The JSON stored in ``app.inbound_raw_imports.scan_verdict``.

        Redacted by construction: this carries counts, types and bounds, and no
        content whatsoever. It is the same object the console and MCP read, so
        there is one description of what happened rather than two that can drift.
        """
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "detected_type": self.detected_type,
            "declared_type": self.declared_type,
            "size_bytes": self.size_bytes,
            "malware": self.malware,
            "malware_engine": self.malware_engine,
            "policy": dict(self.policy),
            "malware_detail": self.malware_detail,
            "evidence": dict(self.evidence),
        }


# ---------------------------------------------------------------------------
# Detection.
# ---------------------------------------------------------------------------


def _detect_content_type_legacy(data: bytes, *, declared_type: str | None = None) -> str:
    """Detect a media type from the BYTES, independent of any claim.

    ``declared_type`` is accepted only to be echoed back for comparison; it can
    never influence the answer. That is the whole contract of E38-NFR02: "no
    trust in filenames, headers, labels, samples, or MIME claims".

    Returns a media type, ``text/csv`` for something that looks like bounded
    text, or ``application/octet-stream`` when nothing can be asserted. It never
    guesses a specific format from a weak signal.
    """
    if not data:
        return "application/octet-stream"
    for prefix, media_type in _MAGIC:
        if data.startswith(prefix):
            return media_type
    head = data[:_ENVELOPE_HEAD_BYTES]
    if b"\x00" in head:
        return "application/octet-stream"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            head.decode("latin-1")
        except UnicodeDecodeError:
            return "application/octet-stream"
    # Decodable, no NULs: text. We say text/csv rather than text/plain because
    # the tabular parsers are the only consumers, and text/plain would suggest a
    # capability nothing here provides.
    return "text/csv"


def _types_conflict(declared: str | None, detected: str) -> bool:
    """Is the claim incompatible with the evidence?

    Deliberately narrow. A sender that says ``application/octet-stream`` or
    nothing at all is not lying, it is unspecific, and refusing those would
    reject most legitimate traffic. What is refused is a claim that is
    positively contradicted -- text claimed for a container, or a container
    claimed for text.
    """
    if not declared:
        return False
    declared = declared.split(";", 1)[0].strip().lower()
    if declared in ("application/octet-stream", "binary/octet-stream", ""):
        return False
    text_like = {"text/csv", "text/plain", "text/tab-separated-values", "application/csv"}
    if declared in text_like and detected not in text_like:
        return True
    if detected in text_like and declared not in text_like:
        # An XLSX declared as such but arriving as plain text, for instance.
        excel_like = {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
            "application/zip",
        }
        return declared in excel_like
    return False


# ---------------------------------------------------------------------------
# Archive bounds.
# ---------------------------------------------------------------------------


def _inspect_archive(
    data: bytes,
    *,
    max_entries: int,
    max_uncompressed: int,
    max_ratio: float,
) -> tuple[str | None, dict[str, Any]]:
    """Bound a ZIP container WITHOUT extracting it.

    Returns ``(reason_or_None, evidence)``. Only the central directory is read:
    entry names, declared sizes and compressed sizes. Nothing is decompressed,
    so a bomb cannot be triggered by the act of measuring it -- which is the
    mistake that makes naive scanners the vector they were meant to stop.
    """
    evidence: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return REASON_ARCHIVE_CORRUPT, {"archive_readable": False}

    evidence["archive_readable"] = True
    evidence["archive_entries"] = len(infos)

    if len(infos) > max_entries:
        evidence["archive_entry_limit"] = max_entries
        return REASON_ARCHIVE_TOO_MANY_ENTRIES, evidence

    total_uncompressed = 0
    worst_ratio = 0.0
    for info in infos:
        name = info.filename or ""
        # An entry name is untrusted data, exactly like a delivered filename.
        # It is never used as a path here -- nothing is extracted -- but a
        # traversal pattern is still evidence of intent, and refusing it costs
        # nothing.
        if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
            evidence["archive_traversal_entry"] = True
            return REASON_ARCHIVE_TRAVERSAL, evidence

        total_uncompressed += int(info.file_size or 0)
        if info.compress_size:
            ratio = float(info.file_size or 0) / float(info.compress_size)
            worst_ratio = max(worst_ratio, ratio)

    evidence["archive_uncompressed_bytes"] = total_uncompressed
    evidence["archive_worst_ratio"] = round(worst_ratio, 2)

    if total_uncompressed > max_uncompressed:
        evidence["archive_uncompressed_limit"] = max_uncompressed
        return REASON_ARCHIVE_TOO_LARGE, evidence
    if worst_ratio > max_ratio:
        evidence["archive_ratio_limit"] = max_ratio
        return REASON_ARCHIVE_RATIO, evidence
    return None, evidence


def _column_envelope(data: bytes) -> int:
    """Count the separators of the first line, over a BOUNDED head.

    Not parsing: no quoting rules, no type inference, no rows. The question is
    only "is this plausibly a table", and the cheapest wrong answer here costs
    far less than the cheapest right answer inside a parser.
    """
    head = data[:_ENVELOPE_HEAD_BYTES]
    newline = head.find(b"\n")
    first = head if newline < 0 else head[:newline]
    best = 0
    for sep in (b",", b";", b"\t", b"|"):
        best = max(best, first.count(sep) + 1)
    return best


# ---------------------------------------------------------------------------
# The scan.
# ---------------------------------------------------------------------------


def _scan_bytes_legacy(
    data: bytes,
    *,
    declared_type: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
    max_archive_entries: int = DEFAULT_MAX_ARCHIVE_ENTRIES,
    max_columns: int = DEFAULT_MAX_COLUMNS,
    malware_scanner=None,  # noqa: ANN001 -- optional callable(bytes) -> (str, str|None)
) -> ScanVerdict:
    """Decide whether these bytes may reach a parser. Pure; never raises.

    Checks run cheapest-first and STOP at the first rejection, so a file that is
    both empty and mistyped reports the reason an operator can act on rather
    than the last one evaluated.

    ``malware_scanner`` is an injection point, not a promise: when None -- which
    is every deployment today -- the verdict says ``malware="unavailable"``
    rather than implying a clean result. A caller that wires a real scanner
    passes a callable returning ``("clean"|"infected", detail)``.
    """
    size = len(data)

    def _reject(reason: str, detected: str, evidence: dict) -> ScanVerdict:
        return ScanVerdict(
            accepted=False,
            reason=reason,
            detected_type=detected,
            declared_type=declared_type,
            size_bytes=size,
            evidence=evidence,
        )

    # 1. Size, both ends.
    if size == 0:
        return _reject(REASON_EMPTY, "application/octet-stream", {"size_bytes": 0})
    if size > max_bytes:
        return _reject(
            REASON_TOO_LARGE,
            "application/octet-stream",
            {"size_bytes": size, "size_limit": max_bytes},
        )

    # 2. What is it, on the evidence?
    detected = detect_content_type(data, declared_type=declared_type)
    evidence: dict[str, Any] = {"size_bytes": size}

    # 3. Does the claim contradict the evidence?
    if _types_conflict(declared_type, detected):
        evidence["declared_type"] = declared_type
        evidence["detected_type"] = detected
        return _reject(REASON_TYPE_MISMATCH, detected, evidence)

    # 4. Could a parser even accept this shape?
    if detected not in _PARSEABLE:
        evidence["detected_type"] = detected
        return _reject(REASON_UNSUPPORTED_TYPE, detected, evidence)

    # 5. Containers: bound before anything opens them.
    if detected in _ZIP_LIKE:
        reason, archive_evidence = _inspect_archive(
            data,
            max_entries=max_archive_entries,
            max_uncompressed=max_uncompressed_bytes,
            max_ratio=max_compression_ratio,
        )
        evidence.update(archive_evidence)
        if reason is not None:
            return _reject(reason, detected, evidence)

    # 6. Text that is not text.
    if detected == "text/csv" and b"\x00" in data[:_ENVELOPE_HEAD_BYTES]:
        return _reject(REASON_NULL_BYTES, detected, evidence)

    # 7. Envelope.
    if detected == "text/csv":
        columns = _column_envelope(data)
        evidence["column_estimate"] = columns
        if columns > max_columns:
            evidence["column_limit"] = max_columns
            return _reject(REASON_TOO_MANY_COLUMNS, detected, evidence)

    # 8. Malware -- reported honestly whether or not a scanner exists.
    malware, malware_detail = "unavailable", "no scanner configured"
    if malware_scanner is not None:
        try:
            malware, malware_detail = malware_scanner(data)
        except Exception as exc:  # noqa: BLE001 -- a broken scanner is not a clean file
            logger.warning("inbound_scan: malware scanner failed: %s", type(exc).__name__)
            malware, malware_detail = "unavailable", "scanner error"
        if malware == "infected":
            return ScanVerdict(
                accepted=False,
                reason="malware_detected",
                detected_type=detected,
                declared_type=declared_type,
                size_bytes=size,
                malware=malware,
                malware_detail=malware_detail,
                evidence=evidence,
            )

    return ScanVerdict(
        accepted=True,
        reason=None,
        detected_type=detected,
        declared_type=declared_type,
        size_bytes=size,
        malware=malware,
        malware_detail=malware_detail,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Display -- the one place an untrusted string is shown.
# ---------------------------------------------------------------------------

#: Characters that make a spreadsheet cell executable when a verdict is exported.
#: Exported (no underscore): the CSV parser refuses cells led by the SAME
#: characters -- one definition, or the two gates would diverge.
FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def _decode_text(data: bytes) -> tuple[str, str] | None:
    if data.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif data.startswith(codecs.BOM_UTF16_LE):
        encoding = "utf-16-le"
    elif data.startswith(codecs.BOM_UTF16_BE):
        encoding = "utf-16-be"
    else:
        encoding = "utf-8"
    try:
        text_value = data.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None
    has_unsafe_control = any(
        unicodedata.category(char) == "Cc" and char not in {"\t", "\r", "\n"} for char in text_value
    )
    if has_unsafe_control:
        return None
    if encoding in {"utf-8", "utf-8-sig"} and "\x00" in text_value:
        return None
    return text_value, encoding


_MIME_ALIASES = {
    "application/csv": "text/csv",
    "text/plain": "text/csv",
    "text/tab-separated-values": "text/csv",
    "application/vnd.spss-sav": "application/x-spss-sav",
}
_UNSPECIFIC_MIME = {"", "application/octet-stream", "binary/octet-stream"}


def _normalise_mime(value: str | None) -> str | None:
    if value is None:
        return None
    normalised = value.split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(normalised, normalised)


def detect_content_type(data: bytes, *, declared_type: str | None = None) -> str:
    del declared_type
    if not data:
        return "application/octet-stream"
    for prefix, media_type in _MAGIC:
        if data.startswith(prefix):
            return media_type
    return "text/csv" if _decode_text(data) is not None else "application/octet-stream"


def _types_conflict_v2(declared: str | None, detected: str) -> bool:
    claim = _normalise_mime(declared)
    if claim is None or claim in _UNSPECIFIC_MIME:
        return False
    allowed = {
        "text/csv": {"text/csv"},
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/zip",
        },
        "application/x-spss-sav": {"application/x-spss-sav"},
    }
    return claim not in allowed.get(detected, set())


def _zip_declared_entry_count(data: bytes) -> int | None:
    tail = data[-65557:]
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        return None
    return int.from_bytes(tail[offset + 10 : offset + 12], "little")


def _preflight_zip_central_directory(
    data: bytes, *, max_entries: int, max_memory_bytes: int
) -> tuple[str | None, int, int]:
    tail = data[-65557:]
    relative_eocd = tail.rfind(b"PK\x05\x06")
    if relative_eocd < 0 or len(tail) - relative_eocd < 22:
        return REASON_ARCHIVE_CORRUPT, 0, 0
    eocd = len(data) - len(tail) + relative_eocd
    disk, central_disk, disk_entries, total_entries = struct.unpack_from("<HHHH", data, eocd + 4)
    central_size, central_offset = struct.unpack_from("<II", data, eocd + 12)
    comment_size = struct.unpack_from("<H", data, eocd + 20)[0]
    if (
        disk != 0
        or central_disk != 0
        or disk_entries != total_entries
        or total_entries == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
        or eocd + 22 + comment_size != len(data)
    ):
        return REASON_ARCHIVE_CORRUPT, total_entries, central_size
    directory_limit = min(max_memory_bytes // 8, max_entries * 4096)
    if central_size > directory_limit:
        return REASON_ARCHIVE_CENTRAL_DIRECTORY, total_entries, central_size
    if total_entries > max_entries:
        return REASON_ARCHIVE_TOO_MANY_ENTRIES, total_entries, central_size
    end = central_offset + central_size
    if central_offset < 0 or end != eocd:
        return REASON_ARCHIVE_CORRUPT, total_entries, central_size
    position = central_offset
    observed_entries = 0
    while position < end:
        if position + 46 > end or data[position : position + 4] != b"PK\x01\x02":
            return REASON_ARCHIVE_CORRUPT, observed_entries, central_size
        name_size, extra_size, entry_comment_size = struct.unpack_from("<HHH", data, position + 28)
        position += 46 + name_size + extra_size + entry_comment_size
        observed_entries += 1
        if observed_entries > max_entries or position > end:
            return REASON_ARCHIVE_TOO_MANY_ENTRIES, observed_entries, central_size
    if position != end or observed_entries != total_entries:
        return REASON_ARCHIVE_CORRUPT, observed_entries, central_size
    return None, observed_entries, central_size


def _xlsx_dimensions_legacy(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
) -> tuple[int, int]:
    max_rows = 0
    max_columns = 0
    dimension_re = re.compile(rb'<dimension[^>]+ref="[A-Z]+\d+(?::([A-Z]+)(\d+))?"')
    for info in infos:
        if not info.filename.startswith("xl/worksheets/") or not info.filename.endswith(".xml"):
            continue
        with archive.open(info) as stream:
            limit = min(int(info.file_size), _ENVELOPE_HEAD_BYTES * 4)
            head = stream.read(limit + 1)
        match = dimension_re.search(head)
        if match:
            end_col = (match.group(1) or b"A").decode("ascii")
            end_row = int(match.group(2) or b"1")
            columns = 0
            for char in end_col:
                columns = columns * 26 + ord(char) - 64
            max_rows = max(max_rows, end_row)
            max_columns = max(max_columns, columns)
        else:
            max_rows = max(max_rows, head.count(b"<row"))
            max_columns = max(max_columns, head.count(b"<c"))
            if len(head) > limit:
                raise ScanRetryableError(
                    REASON_SCAN_MEMORY,
                    evidence={"spreadsheet_dimension_bounded": False},
                )
    return max_rows, max_columns


def _column_index(reference: str) -> int:
    column = 0
    for char in reference:
        if not "A" <= char <= "Z":
            break
        column = column * 26 + ord(char) - 64
    return column


def _xlsx_linked_sheets(
    archive: zipfile.ZipFile,
    names: set[str],
    *,
    deadline: float,
) -> set[str]:
    """Validate exact OOXML metadata roots, namespaces, relationships and types."""
    content_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    sheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    office_rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    workbook_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    worksheet_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
    metadata_limit = 1024 * 1024

    def parse_metadata(name: str, expected_root: str) -> etree.Element:
        if time.monotonic() > deadline:
            raise ScanRetryableError(REASON_SCAN_TIMEOUT)
        info = archive.getinfo(name)
        if info.file_size > metadata_limit:
            raise ValueError("xlsx metadata exceeds its bounded envelope")
        with archive.open(info) as stream:
            root = etree.parse(stream).getroot()
        if root.tag != expected_root:
            raise ValueError("xlsx XML root or namespace is invalid")
        return root

    content_types = parse_metadata("[Content_Types].xml", f"{{{content_ns}}}Types")
    overrides = {
        element.attrib.get("PartName", "").lstrip("/"): element.attrib.get("ContentType", "")
        for element in content_types.iter(f"{{{content_ns}}}Override")
    }
    if overrides.get("xl/workbook.xml") != workbook_type:
        raise ValueError("xlsx workbook content type is missing")

    package_rels = parse_metadata("_rels/.rels", f"{{{package_rel_ns}}}Relationships")
    office_targets = [
        element.attrib.get("Target", "").lstrip("/")
        for element in package_rels.iter(f"{{{package_rel_ns}}}Relationship")
        if element.attrib.get("Type") == f"{office_rel_ns}/officeDocument"
        and element.attrib.get("TargetMode", "Internal") == "Internal"
    ]
    if office_targets != ["xl/workbook.xml"]:
        raise ValueError("xlsx package relationship is invalid")

    workbook = parse_metadata("xl/workbook.xml", f"{{{sheet_ns}}}workbook")
    relationship_ids = [
        element.attrib.get(f"{{{office_rel_ns}}}id")
        for element in workbook.iter(f"{{{sheet_ns}}}sheet")
    ]
    if not relationship_ids or any(not value for value in relationship_ids):
        raise ValueError("xlsx workbook has no linked sheet")

    workbook_rels = parse_metadata(
        "xl/_rels/workbook.xml.rels", f"{{{package_rel_ns}}}Relationships"
    )
    relationships = {
        element.attrib.get("Id"): (
            element.attrib.get("Target", ""),
            element.attrib.get("Type", ""),
            element.attrib.get("TargetMode", "Internal"),
        )
        for element in workbook_rels.iter(f"{{{package_rel_ns}}}Relationship")
    }
    linked: set[str] = set()
    for relationship_id in relationship_ids:
        target, rel_type, target_mode = relationships.get(relationship_id, ("", "", ""))
        if rel_type != f"{office_rel_ns}/worksheet" or target_mode != "Internal" or not target:
            raise ValueError("xlsx sheet relationship is invalid")
        resolved = posixpath.normpath(
            target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target)
        )
        if resolved.startswith("../") or resolved not in names:
            raise ValueError("xlsx sheet target is missing")
        if overrides.get(resolved) != worksheet_type:
            raise ValueError("xlsx sheet content type is missing")
        linked.add(resolved)
    return linked


class _XMLTokenBudget:
    """Bound XML lexical tokens before an incremental parser can buffer them."""

    _SPECIAL_PREFIXES = (b"<!--", b"<![CDATA[", b"<?", b"<!DOCTYPE")

    def __init__(self, limit: int):
        self.limit = limit
        self.state = "text"
        self.count = 0
        self.quote: int | None = None
        self.prefix = bytearray()
        self.tail = bytearray()

    def _increment(self) -> None:
        self.count += 1
        if self.count > self.limit:
            raise ValueError("xlsx XML token exceeds its bounded envelope")

    def _start_markup(self) -> None:
        self.state = "open"
        self.count = 1
        self.quote = None
        self.prefix = bytearray(b"<")
        self.tail.clear()

    def _finish(self) -> None:
        self.state = "text"
        self.count = 0
        self.quote = None
        self.prefix.clear()
        self.tail.clear()

    def feed(self, chunk: bytes) -> None:
        for value in chunk:
            if self.state == "text":
                if value == ord("<"):
                    self._start_markup()
                else:
                    self._increment()
                continue

            self._increment()
            if self.state == "open":
                self.prefix.append(value)
                upper_prefix = bytes(self.prefix).upper()
                if upper_prefix == b"<!DOCTYPE":
                    raise ValueError("xlsx document types are forbidden")
                if bytes(self.prefix) == b"<!--":
                    self.state = "comment"
                    self.tail = bytearray(self.prefix[-3:])
                    continue
                if bytes(self.prefix) == b"<![CDATA[":
                    self.state = "cdata"
                    self.tail = bytearray(self.prefix[-3:])
                    continue
                if bytes(self.prefix) == b"<?":
                    self.state = "pi"
                    self.tail = bytearray(self.prefix[-2:])
                    continue
                candidates = [
                    candidate
                    for candidate in self._SPECIAL_PREFIXES
                    if candidate.startswith(bytes(self.prefix))
                    or candidate.startswith(upper_prefix)
                ]
                if candidates:
                    continue
                self.state = "tag"

            if self.state == "tag":
                if self.quote is not None:
                    if value == self.quote:
                        self.quote = None
                elif value in {ord("'"), ord('"')}:
                    self.quote = value
                elif value == ord(">"):
                    self._finish()
                continue

            self.tail.append(value)
            keep = 3 if self.state in {"comment", "cdata"} else 2
            if len(self.tail) > keep:
                del self.tail[:-keep]
            if self.state == "comment" and self.tail.endswith(b"-->"):
                self._finish()
            elif self.state == "cdata" and self.tail.endswith(b"]]>"):
                self._finish()
            elif self.state == "pi" and self.tail.endswith(b"?>"):
                self._finish()


def _xlsx_dimensions(
    archive: zipfile.ZipFile,
    sheet_names: set[str],
    *,
    max_rows: int,
    max_columns: int,
    max_token_bytes: int,
    deadline: float,
) -> tuple[int, int]:
    """Count worksheet dimensions with an event-only parser and bounded depth."""
    import xml.parsers.expat as expat  # noqa: PLC0415

    sheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    worksheet_tag = sheet_ns + "}worksheet"
    row_tag = sheet_ns + "}row"
    cell_tag = sheet_ns + "}c"
    observed_rows = 0
    physical_rows = 0
    observed_columns = 0

    for name in sorted(sheet_names):
        current_cells = 0
        depth = 0
        seen_root = False
        parser = expat.ParserCreate(namespace_separator="}")
        token_budget = _XMLTokenBudget(max_token_bytes)

        def reject_declaration(*_args) -> None:
            raise ValueError("xlsx declarations and entities are forbidden")

        def start_element(element_name: str, attributes: dict[str, str]) -> None:
            nonlocal current_cells
            nonlocal depth
            nonlocal observed_columns
            nonlocal observed_rows
            nonlocal physical_rows
            nonlocal seen_root
            if not seen_root:
                if element_name != worksheet_tag:
                    raise ValueError("xlsx worksheet root or namespace is invalid")
                seen_root = True
            depth += 1
            if depth > 64:
                raise ValueError("xlsx XML nesting exceeds its bounded envelope")
            if element_name == row_tag:
                physical_rows += 1
                current_cells = 0
                row_ref = attributes.get("r", "")
                observed_rows = max(
                    observed_rows,
                    physical_rows,
                    int(row_ref) if row_ref.isdigit() else 0,
                )
            elif element_name == cell_tag:
                current_cells += 1
                cell_ref = attributes.get("r", "")
                observed_columns = max(
                    observed_columns,
                    current_cells,
                    _column_index(cell_ref.upper()),
                )

        def end_element(_element_name: str) -> None:
            nonlocal depth
            depth -= 1
            if depth < 0:
                raise ValueError("xlsx XML nesting is invalid")

        parser.StartElementHandler = start_element
        parser.EndElementHandler = end_element
        parser.StartDoctypeDeclHandler = reject_declaration
        parser.EntityDeclHandler = reject_declaration
        parser.ExternalEntityRefHandler = lambda *_args: 0

        try:
            with archive.open(name) as stream:
                while chunk := stream.read(64 * 1024):
                    if time.monotonic() > deadline:
                        raise ScanRetryableError(REASON_SCAN_TIMEOUT)
                    token_budget.feed(chunk)
                    parser.Parse(chunk, False)
                    if observed_rows > max_rows or observed_columns > max_columns:
                        return observed_rows, observed_columns
                parser.Parse(b"", True)
        except expat.ExpatError as exc:
            raise ValueError("xlsx worksheet XML is invalid") from exc
        if not seen_root or depth != 0:
            raise ValueError("xlsx worksheet XML is incomplete")
    return observed_rows, observed_columns


def _inspect_archive_v2(
    data: bytes,
    *,
    max_entries: int,
    max_uncompressed: int,
    max_ratio: float,
    max_rows: int,
    max_columns: int,
    deadline: float,
    max_memory_bytes: int,
) -> tuple[str | None, dict[str, Any], str]:
    evidence: dict[str, Any] = {}
    central_reason, central_entries, central_size = _preflight_zip_central_directory(
        data, max_entries=max_entries, max_memory_bytes=max_memory_bytes
    )
    evidence.update(
        {
            "archive_entries": central_entries,
            "archive_entry_limit": max_entries,
            "archive_central_directory_bytes": central_size,
            "archive_central_directory_limit": min(max_memory_bytes // 8, max_entries * 4096),
        }
    )
    if central_reason is not None:
        if central_reason == REASON_ARCHIVE_CORRUPT:
            evidence["archive_readable"] = False
        return central_reason, evidence, "application/zip"
    if time.monotonic() > deadline:
        raise ScanRetryableError(REASON_SCAN_TIMEOUT, evidence=evidence)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = {info.filename for info in infos}
            evidence["archive_readable"] = True
            total_uncompressed = 0
            worst_ratio = 0.0
            for info in infos:
                if time.monotonic() > deadline:
                    raise ScanRetryableError(REASON_SCAN_TIMEOUT, evidence=evidence)
                name = info.filename or ""
                normalised = name.replace("\\", "/")
                if (
                    name.startswith(("/", "\\"))
                    or re.match(r"^[A-Za-z]:", name)
                    or normalised.startswith("//")
                    or ".." in normalised.split("/")
                ):
                    evidence["archive_traversal_entry"] = True
                    return REASON_ARCHIVE_TRAVERSAL, evidence, "application/zip"
                if info.flag_bits & 0x1:
                    evidence["archive_encrypted_entry"] = True
                    return REASON_ARCHIVE_ENCRYPTED, evidence, "application/zip"
                uncompressed = int(info.file_size or 0)
                compressed = int(info.compress_size or 0)
                total_uncompressed += uncompressed
                if uncompressed and compressed == 0:
                    evidence.update(
                        {
                            "archive_worst_ratio": "infinite",
                            "archive_ratio_limit": max_ratio,
                        }
                    )
                    return REASON_ARCHIVE_RATIO, evidence, "application/zip"
                if compressed:
                    worst_ratio = max(worst_ratio, uncompressed / compressed)
            evidence.update(
                {
                    "archive_uncompressed_bytes": total_uncompressed,
                    "archive_uncompressed_limit": max_uncompressed,
                    "archive_worst_ratio": round(worst_ratio, 2),
                    "archive_ratio_limit": max_ratio,
                }
            )
            if total_uncompressed > max_uncompressed:
                return REASON_ARCHIVE_TOO_LARGE, evidence, "application/zip"
            if len(data) + total_uncompressed > max_memory_bytes:
                raise ScanRetryableError(REASON_SCAN_MEMORY, evidence=evidence)
            if worst_ratio > max_ratio:
                return REASON_ARCHIVE_RATIO, evidence, "application/zip"
            required = {
                "[Content_Types].xml",
                "_rels/.rels",
                "xl/workbook.xml",
                "xl/_rels/workbook.xml.rels",
            }
            has_sheet = any(
                name.startswith("xl/worksheets/") and name.endswith(".xml") for name in names
            )
            if not required.issubset(names) or not has_sheet:
                return REASON_ARCHIVE_NOT_SPREADSHEET, evidence, "application/zip"
            try:
                linked_sheets = _xlsx_linked_sheets(archive, names, deadline=deadline)
                rows, columns = _xlsx_dimensions(
                    archive,
                    linked_sheets,
                    max_rows=max_rows,
                    max_columns=max_columns,
                    max_token_bytes=max(
                        1024,
                        min(1024 * 1024, max_memory_bytes // 16),
                    ),
                    deadline=deadline,
                )
            except (KeyError, ValueError):
                return REASON_ARCHIVE_NOT_SPREADSHEET, evidence, "application/zip"
    except ScanRetryableError:
        raise
    except (zipfile.BadZipFile, etree.ParseError, OSError, RuntimeError, ValueError, KeyError):
        return REASON_ARCHIVE_CORRUPT, {"archive_readable": False}, "application/zip"
    detected = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    evidence.update(
        {
            "spreadsheet_rows": rows,
            "row_limit": max_rows,
            "spreadsheet_columns": columns,
            "column_limit": max_columns,
        }
    )
    if rows > max_rows:
        return REASON_TOO_MANY_ROWS, evidence, detected
    if columns > max_columns:
        return REASON_TOO_MANY_COLUMNS, evidence, detected
    return None, evidence, detected


def _text_envelope_v2(
    text_value: str,
    *,
    data_size: int,
    max_rows: int,
    max_columns: int,
    deadline: float,
) -> tuple[str | None, dict[str, Any]]:
    evidence: dict[str, Any] = {}
    if not text_value.strip("\ufeff \t\r\n"):
        return REASON_WHITESPACE_ONLY, evidence
    inspection = text_value[: _ENVELOPE_HEAD_BYTES + 1]
    in_quote = False
    header_end: int | None = None
    index = 0
    while index < len(inspection):
        if index % 4096 == 0 and time.monotonic() > deadline:
            raise ScanRetryableError(REASON_SCAN_TIMEOUT, evidence=evidence)
        char = inspection[index]
        if char == '"':
            if in_quote and index + 1 < len(inspection) and inspection[index + 1] == '"':
                index += 2
                continue
            in_quote = not in_quote
        elif char in "\r\n" and not in_quote:
            header_end = index
            break
        index += 1
    if header_end is None and len(text_value) > _ENVELOPE_HEAD_BYTES:
        return REASON_HEADER_TOO_LARGE, {
            "header_inspection_bytes": _ENVELOPE_HEAD_BYTES,
            "size_bytes": data_size,
            "header_terminated": False,
        }
    header = text_value if header_end is None else text_value[:header_end]
    best = 0
    for delimiter in (",", ";", "\t", "|"):
        try:
            fields = next(csv.reader(io.StringIO(header), delimiter=delimiter, strict=True))
        except (csv.Error, StopIteration):
            continue
        best = max(best, len(fields))
    if best == 0 or in_quote:
        return REASON_HEADER_TOO_LARGE, {
            "header_inspection_bytes": min(len(inspection), _ENVELOPE_HEAD_BYTES),
            "size_bytes": data_size,
            "header_terminated": False,
        }
    evidence.update({"column_estimate": best, "column_limit": max_columns})
    if best > max_columns:
        return REASON_TOO_MANY_COLUMNS, evidence
    rows = 0
    in_quote = False
    index = 0
    while index < len(text_value):
        if index % 4096 == 0 and time.monotonic() > deadline:
            raise ScanRetryableError(REASON_SCAN_TIMEOUT, evidence=evidence)
        char = text_value[index]
        if char == '"':
            if in_quote and index + 1 < len(text_value) and text_value[index + 1] == '"':
                index += 2
                continue
            in_quote = not in_quote
        elif char in "\r\n" and not in_quote:
            rows += 1
            if char == "\r" and index + 1 < len(text_value) and text_value[index + 1] == "\n":
                index += 1
            if rows > max_rows:
                break
        index += 1
    if text_value and text_value[-1] not in "\r\n":
        rows += 1
    if in_quote:
        return REASON_HEADER_TOO_LARGE, {
            **evidence,
            "header_inspection_bytes": min(len(text_value), _ENVELOPE_HEAD_BYTES),
            "header_terminated": False,
        }
    evidence.update({"row_estimate": rows, "row_limit": max_rows})
    if rows > max_rows:
        return REASON_TOO_MANY_ROWS, evidence
    return None, evidence


def _sav_dimensions(data: bytes, *, deadline: float) -> tuple[int, int]:
    """Read the bounded SAV dictionary and return logical rows/variables."""
    if len(data) < 216 or data[:4] not in {b"$FL2", b"$FL3"}:
        return -1, -1
    layout_le = struct.unpack_from("<i", data, 64)[0]
    layout_be = struct.unpack_from(">i", data, 64)[0]
    if layout_le in {2, 3}:
        endian = "<"
    elif layout_be in {2, 3}:
        endian = ">"
    else:
        return -1, -1
    storage_slots = struct.unpack_from(f"{endian}i", data, 68)[0]
    compression = struct.unpack_from(f"{endian}i", data, 72)[0]
    rows = struct.unpack_from(f"{endian}i", data, 80)[0]
    if storage_slots <= 0 or compression not in {0, 1, 2} or rows < -1:
        return -1, -1

    offset = 176
    seen_slots = 0
    logical_columns = 0
    long_string_roots: set[bytes] = set()
    while offset + 4 <= len(data) and struct.unpack_from(f"{endian}i", data, offset)[0] == 2:
        if time.monotonic() > deadline:
            raise ScanRetryableError(REASON_SCAN_TIMEOUT)
        if offset + 32 > len(data):
            return -1, -1
        variable_type = struct.unpack_from(f"{endian}i", data, offset + 4)[0]
        has_label = struct.unpack_from(f"{endian}i", data, offset + 8)[0]
        missing_code = struct.unpack_from(f"{endian}i", data, offset + 12)[0]
        if (
            variable_type < -1
            or variable_type > 32767
            or has_label not in {0, 1}
            or missing_code not in {-3, -2, -1, 0, 1, 2, 3}
        ):
            return -1, -1
        variable_name = data[offset + 24 : offset + 32].rstrip(b" \x00")
        is_long_segment = any(
            variable_name.startswith(root) and variable_name[len(root) :].isdigit()
            for root in long_string_roots
        )
        if variable_type >= 0 and not variable_name.startswith(b"$@") and not is_long_segment:
            logical_columns += 1
        if variable_type == 255 and variable_name:
            long_string_roots.add(variable_name)
        offset += 32
        if has_label:
            if offset + 4 > len(data):
                return -1, -1
            label_bytes = struct.unpack_from(f"{endian}i", data, offset)[0]
            if label_bytes < 0 or label_bytes > 65535:
                return -1, -1
            offset += 4 + ((label_bytes + 3) // 4) * 4
        offset += abs(missing_code) * 8
        if offset > len(data):
            return -1, -1
        seen_slots += 1
    if seen_slots != storage_slots or logical_columns <= 0:
        return -1, -1

    terminator = None
    while offset + 4 <= len(data):
        if time.monotonic() > deadline:
            raise ScanRetryableError(REASON_SCAN_TIMEOUT)
        record_type = struct.unpack_from(f"{endian}i", data, offset)[0]
        if record_type == 999:
            if offset + 8 > len(data) or struct.unpack_from(f"{endian}i", data, offset + 4)[0] != 0:
                return -1, -1
            terminator = offset
            break
        if record_type == 3:
            if offset + 8 > len(data):
                return -1, -1
            count = struct.unpack_from(f"{endian}i", data, offset + 4)[0]
            if count < 0 or count > 1_000_000:
                return -1, -1
            offset += 8
            for index in range(count):
                if index % 4096 == 0 and time.monotonic() > deadline:
                    raise ScanRetryableError(REASON_SCAN_TIMEOUT)
                if offset + 9 > len(data):
                    return -1, -1
                label_length = data[offset + 8]
                offset += 8 + ((label_length + 1 + 7) // 8) * 8
                if offset > len(data):
                    return -1, -1
            continue
        if record_type == 4:
            if offset + 8 > len(data):
                return -1, -1
            count = struct.unpack_from(f"{endian}i", data, offset + 4)[0]
            if count < 0 or count > 1_000_000 or offset + 8 + count * 4 > len(data):
                return -1, -1
            offset += 8 + count * 4
            continue
        if record_type == 6:
            if offset + 8 > len(data):
                return -1, -1
            count = struct.unpack_from(f"{endian}i", data, offset + 4)[0]
            if count < 0 or count > 100_000 or offset + 8 + count * 80 > len(data):
                return -1, -1
            offset += 8 + count * 80
            continue
        if record_type == 7:
            if offset + 16 > len(data):
                return -1, -1
            size, count = struct.unpack_from(f"{endian}ii", data, offset + 8)
            if size < 0 or count < 0 or size * count > len(data) - offset - 16:
                return -1, -1
            offset += 16 + size * count
            continue
        return -1, -1
    if terminator is None:
        return -1, -1
    available = len(data) - terminator - 8
    if rows > 0:
        if compression == 0 and available < rows * storage_slots * 8:
            return -1, -1
        if compression == 1 and available < 8:
            return -1, -1
        if compression == 2 and available < 2:
            return -1, -1
    return rows, logical_columns


class ClamAVInstreamScanner:
    version = DEFAULT_MALWARE_ENGINE

    def __init__(self, host: str, port: int, timeout_seconds: float) -> None:
        self.host = host
        self.port = port
        self.timeout_seconds = timeout_seconds
        self.deadline = time.monotonic() + timeout_seconds

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ScanRetryableError(REASON_SCAN_TIMEOUT)
        return remaining

    def _engine_version(self) -> str:
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self._remaining()
            ) as client:
                client.settimeout(self._remaining())
                client.sendall(b"zVERSION\0")
                response = client.recv(512)
        except ScanRetryableError:
            raise
        except (OSError, TimeoutError) as exc:
            raise ScanRetryableError(REASON_MALWARE_UNAVAILABLE) from exc
        match = re.search(rb"ClamAV\s+([0-9.]+)/([0-9]+)/", response)
        if match is None:
            raise ScanRetryableError(REASON_MALWARE_INVALID)
        engine = match.group(1).decode("ascii")
        signatures = match.group(2).decode("ascii")
        return f"clamav-{engine}-db-{signatures}"

    def __call__(self, data: bytes) -> tuple[str, str, str]:
        engine_version = self._engine_version()
        try:
            with socket.create_connection(
                (self.host, self.port),
                timeout=self._remaining(),
            ) as client:
                client.settimeout(self._remaining())
                client.sendall(b"zINSTREAM\0")
                for offset in range(0, len(data), 64 * 1024):
                    chunk = data[offset : offset + 64 * 1024]
                    client.settimeout(self._remaining())
                    client.sendall(struct.pack("!I", len(chunk)) + chunk)
                client.sendall(struct.pack("!I", 0))
                response = client.recv(4096)
        except (OSError, TimeoutError) as exc:
            raise ScanRetryableError(REASON_MALWARE_UNAVAILABLE) from exc
        if response.endswith(b"OK\0"):
            return "clean", "scanner_clean", engine_version
        if b"FOUND" in response:
            return "infected", "signature_detected", engine_version
        raise ScanRetryableError(REASON_MALWARE_INVALID)


def configured_malware_scanner(
    *,
    timeout_seconds: float = DEFAULT_MAX_SCAN_SECONDS,
) -> ClamAVInstreamScanner:
    host = os.environ.get("INBOUND_CLAMAV_HOST", "").strip()
    raw_port = os.environ.get("INBOUND_CLAMAV_PORT", "3310").strip()
    if not host:
        raise ScanRetryableError(REASON_MALWARE_UNAVAILABLE)
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ScanRetryableError(REASON_MALWARE_UNAVAILABLE) from exc
    if not 1 <= port <= 65535:
        raise ScanRetryableError(REASON_MALWARE_UNAVAILABLE)
    return ClamAVInstreamScanner(host, port, timeout_seconds)


def oversized_object_verdict(
    *,
    declared_size: int,
    declared_type: str | None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    observed_size: int | None = None,
) -> ScanVerdict:
    size = max(declared_size, observed_size or (max_bytes + 1))
    policy = {
        "version": SCAN_POLICY_VERSION,
        "max_bytes": max_bytes,
        "max_uncompressed_bytes": DEFAULT_MAX_UNCOMPRESSED_BYTES,
        "max_compression_ratio": DEFAULT_MAX_COMPRESSION_RATIO,
        "max_archive_entries": DEFAULT_MAX_ARCHIVE_ENTRIES,
        "max_columns": DEFAULT_MAX_COLUMNS,
        "max_rows": DEFAULT_MAX_ROWS,
        "max_scan_seconds": DEFAULT_MAX_SCAN_SECONDS,
        "max_memory_bytes": DEFAULT_MAX_MEMORY_BYTES,
    }
    return ScanVerdict(
        accepted=False,
        reason=REASON_TOO_LARGE,
        detected_type="not_read",
        declared_type=_normalise_mime(declared_type),
        size_bytes=size,
        malware="not_run",
        malware_detail=None,
        malware_engine=DEFAULT_MALWARE_ENGINE,
        policy=policy,
        evidence={"size_bytes": size, "size_limit": max_bytes},
    )


def scan_bytes(
    data: bytes,
    *,
    declared_type: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
    max_archive_entries: int = DEFAULT_MAX_ARCHIVE_ENTRIES,
    max_columns: int = DEFAULT_MAX_COLUMNS,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_scan_seconds: float = DEFAULT_MAX_SCAN_SECONDS,
    max_memory_bytes: int = DEFAULT_MAX_MEMORY_BYTES,
    malware_scanner=None,  # noqa: ANN001
) -> ScanVerdict:
    deadline = time.monotonic() + max_scan_seconds
    size = len(data)
    policy = {
        "version": SCAN_POLICY_VERSION,
        "max_bytes": max_bytes,
        "max_uncompressed_bytes": max_uncompressed_bytes,
        "max_compression_ratio": max_compression_ratio,
        "max_archive_entries": max_archive_entries,
        "max_columns": max_columns,
        "max_rows": max_rows,
        "max_scan_seconds": max_scan_seconds,
        "max_memory_bytes": max_memory_bytes,
    }

    def reject(
        reason: str,
        detected: str,
        evidence: dict[str, Any],
        *,
        malware: str = "not_run",
        malware_detail: str | None = None,
        malware_engine: str = DEFAULT_MALWARE_ENGINE,
    ) -> ScanVerdict:
        return ScanVerdict(
            accepted=False,
            reason=reason,
            detected_type=detected,
            declared_type=_normalise_mime(declared_type),
            size_bytes=size,
            malware=malware,
            malware_detail=malware_detail,
            malware_engine=malware_engine,
            policy=policy,
            evidence=evidence,
        )

    if size == 0:
        return reject(REASON_EMPTY, "application/octet-stream", {"size_bytes": 0})
    if size > max_bytes:
        return reject(
            REASON_TOO_LARGE,
            "application/octet-stream",
            {"size_bytes": size, "size_limit": max_bytes},
        )
    if size * 2 > max_memory_bytes:
        raise ScanRetryableError(
            REASON_SCAN_MEMORY, evidence={"size_bytes": size, "memory_limit": max_memory_bytes}
        )
    detected = detect_content_type(data)
    evidence: dict[str, Any] = {"size_bytes": size, "size_limit": max_bytes}
    claim = _normalise_mime(declared_type)
    if detected == "application/zip" and claim == "text/csv":
        evidence.update({"declared_type": claim, "detected_type": detected})
        return reject(REASON_TYPE_MISMATCH, detected, evidence)
    if detected == "application/octet-stream" and claim == "text/csv" and b"\x00" in data:
        return reject(REASON_NULL_BYTES, detected, evidence)
    if detected == "application/zip":
        reason, archive_evidence, detected = _inspect_archive_v2(
            data,
            max_entries=max_archive_entries,
            max_uncompressed=max_uncompressed_bytes,
            max_ratio=max_compression_ratio,
            max_rows=max_rows,
            max_columns=max_columns,
            deadline=deadline,
            max_memory_bytes=max_memory_bytes,
        )
        evidence.update(archive_evidence)
        if reason is not None:
            return reject(reason, detected, evidence)
    elif detected == "application/vnd.ms-excel":
        return reject(REASON_CONTAINER_UNSUPPORTED, detected, evidence)
    if _types_conflict_v2(declared_type, detected):
        evidence.update(
            {
                "declared_type": _normalise_mime(declared_type),
                "detected_type": detected,
            }
        )
        return reject(REASON_TYPE_MISMATCH, detected, evidence)
    if detected not in _PARSEABLE:
        evidence["detected_type"] = detected
        return reject(REASON_UNSUPPORTED_TYPE, detected, evidence)
    if detected == "text/csv":
        decoded = _decode_text(data)
        if decoded is None:
            return reject(REASON_NULL_BYTES, detected, evidence)
        text_value, encoding = decoded
        evidence["encoding"] = encoding
        reason, envelope = _text_envelope_v2(
            text_value,
            data_size=size,
            max_rows=max_rows,
            max_columns=max_columns,
            deadline=deadline,
        )
        evidence.update(envelope)
        if reason is not None:
            return reject(reason, detected, evidence)
    elif detected == "application/x-spss-sav":
        rows, columns = _sav_dimensions(data, deadline=deadline)
        evidence.update(
            {
                "spreadsheet_rows": rows,
                "row_limit": max_rows,
                "spreadsheet_columns": columns,
                "column_limit": max_columns,
            }
        )
        if rows < 0 or columns <= 0:
            return reject(REASON_DIMENSIONS_UNKNOWN, detected, evidence)
        if rows > max_rows:
            return reject(REASON_TOO_MANY_ROWS, detected, evidence)
        if columns > max_columns:
            return reject(REASON_TOO_MANY_COLUMNS, detected, evidence)
    if time.monotonic() > deadline:
        raise ScanRetryableError(REASON_SCAN_TIMEOUT, evidence=evidence)
    # ── LA LISTE BLANCHE DE FORMATS EST LA BARRIÈRE. Décision Jean, 2026-08-05.
    #
    # Tout ce qui arrive ici a DÉJÀ franchi, plus haut dans cette même fonction :
    # la détection de type réelle (`detect_content_type`, pas l'en-tête déclaré),
    # le refus de tout désaccord entre type déclaré et type détecté, le refus des
    # octets nuls, l'inspection bornée des archives, la liste blanche
    # `_PARSEABLE` — CSV, TSV, XLSX, SAV — et les bornes de lignes et de colonnes.
    # Un fichier qui sort de là est un TABLEAU, et un tableau qu'on parse en
    # lignes n'exécute rien. C'est le contrôle de format qui protège, pas la
    # signature virale posée derrière lui.
    #
    # Ce que ça a coûté tant que la règle était l'inverse : aucun antivirus n'est
    # déployé, `configured_malware_scanner` levait donc `malware_scanner_unavailable`,
    # et TOUT import de fichier répondait 503 — y compris un CSV de cinq lignes
    # déjà reconnu, déjà borné, déjà accepté par tout le reste de ce scan (G9,
    # mesuré le 2026-08-04). Déployer un antivirus de 2 Go pour recevoir un CSV
    # était la réponse disproportionnée.
    #
    # CE QUI N'EST PAS AFFAIBLI : un scanner CONFIGURÉ est toujours consulté, et
    # son verdict fait toujours autorité — `infected` rejette. Seule change
    # l'absence de scanner : elle cesse d'être une panne et devient une politique
    # nommée, inscrite dans l'évidence du verdict pour qu'aucune lecture ne
    # puisse croire qu'une signature a été vérifiée.
    if malware_scanner is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ScanRetryableError(REASON_SCAN_TIMEOUT, evidence=evidence)
        try:
            malware_scanner = configured_malware_scanner(timeout_seconds=remaining)
        except ScanRetryableError:
            evidence["malware_policy"] = MALWARE_POLICY_FORMAT_ALLOWLIST
            evidence["parseable_type"] = detected
            return ScanVerdict(
                accepted=True,
                reason=None,
                detected_type=detected,
                declared_type=_normalise_mime(declared_type),
                size_bytes=size,
                malware="not_run",
                malware_detail=MALWARE_POLICY_FORMAT_ALLOWLIST,
                malware_engine="none",
                policy=policy,
                evidence=evidence,
            )
    engine = str(getattr(malware_scanner, "version", "injected-scanner-v1"))
    if re.fullmatch(r"[A-Za-z0-9._+-]{1,80}", engine) is None:
        engine = "unknown-scanner"
    try:
        malware_result = malware_scanner(data)
    except ScanRetryableError:
        raise
    except Exception as exc:
        logger.warning("inbound_scan: malware scanner failed: %s", type(exc).__name__)
        raise ScanRetryableError(REASON_MALWARE_UNAVAILABLE, evidence=evidence) from exc
    if (
        not isinstance(malware_result, tuple)
        or len(malware_result) not in {2, 3}
        or malware_result[0] not in {"clean", "infected"}
    ):
        raise ScanRetryableError(REASON_MALWARE_INVALID, evidence=evidence)
    malware = malware_result[0]
    if len(malware_result) == 3:
        reported_engine = malware_result[2]
        if (
            not isinstance(reported_engine, str)
            or re.fullmatch(r"[A-Za-z0-9._+-]{1,80}", reported_engine) is None
        ):
            raise ScanRetryableError(REASON_MALWARE_INVALID, evidence=evidence)
        engine = reported_engine

    detail = "scanner_clean" if malware == "clean" else "signature_detected"
    if time.monotonic() > deadline:
        raise ScanRetryableError(REASON_SCAN_TIMEOUT, evidence=evidence)
    if malware == "infected":
        return reject(
            "malware_detected",
            detected,
            evidence,
            malware=malware,
            malware_detail=detail,
            malware_engine=engine,
        )
    return ScanVerdict(
        accepted=True,
        reason=None,
        detected_type=detected,
        declared_type=_normalise_mime(declared_type),
        size_bytes=size,
        malware="clean",
        malware_detail=detail,
        malware_engine=engine,
        policy=policy,
        evidence=evidence,
    )


def neutralise_untrusted(value: str | None, *, max_len: int = 120) -> str | None:
    """Bound and defuse an untrusted string for display.

    Filenames and archive entry names are attacker-controlled. This does three
    things and no more: caps the length, strips control characters, and prefixes
    a leading formula character so a verdict pasted into a spreadsheet stays
    text. It does NOT sanitise for HTML -- the console escapes on render, and a
    second escaping layer here would double-encode legitimate names.
    """
    if value is None:
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable())
    cleaned = cleaned[:max_len]
    if cleaned.startswith(FORMULA_LEADERS):
        cleaned = ("'" + cleaned)[:max_len]
    return cleaned


# ---------------------------------------------------------------------------
# The governed write.
# ---------------------------------------------------------------------------


def scan_raw_import(
    conn,
    *,
    raw_import_id: str,
    datastream_id: str,
    data: bytes,
    declared_type: str | None = None,
    actor: str = "inbound-worker",
    trace_id: str | None = None,
    malware_scanner=None,  # noqa: ANN001
    persist_intent: bool = False,
    dispatch_bundle: dict[str, Any] | None = None,
    dispatch_bundle_resolver=None,  # noqa: ANN001
    **bounds: Any,
) -> ScanVerdict:
    """Scan one raw import and record the verdict on its row.

    Moves the row RECEIVED -> SCANNING -> (ACCEPTED | REJECTED). The SCANNING
    step is written before the work rather than after, so a worker that dies
    mid-scan leaves a row that says what it was doing instead of one that still
    claims RECEIVED.

    A rejection is TERMINAL: the row lands in REJECTED with an actionable
    ``error_code`` and never advances to a parser. It is not an exception -- one
    file being refused must not abort its siblings (38.9 AC1), and callers loop.

    The caller owns the transaction unless ``persist_intent`` is selected by
    the durable job worker, which commits SCANNING before untrusted work.
    """
    from core.inbound_raw_imports import mark_raw_import_state  # noqa: PLC0415

    mark_raw_import_state(
        conn,
        raw_import_id=raw_import_id,
        datastream_id=datastream_id,
        state="SCANNING",
        actor=actor,
        host_context={},
        trace_id=trace_id,
        idempotency_key=f"raw-import-state:{raw_import_id}:SCANNING",
    )
    if persist_intent:
        conn.commit()

    verdict = scan_bytes(
        data,
        declared_type=declared_type,
        malware_scanner=malware_scanner,
        **bounds,
    )

    if verdict.accepted and dispatch_bundle_resolver is not None:
        if dispatch_bundle is not None:
            raise ValueError(
                "dispatch bundle and resolver cannot both be supplied"
            )
        dispatch_bundle = dispatch_bundle_resolver()

    mark_raw_import_state(
        conn,
        raw_import_id=raw_import_id,
        datastream_id=datastream_id,
        state="ACCEPTED" if verdict.accepted else "REJECTED",
        actor=actor,
        host_context={},
        trace_id=trace_id,
        idempotency_key=(
            f"raw-import-state:{raw_import_id}:{'ACCEPTED' if verdict.accepted else 'REJECTED'}"
        ),
        error_code=verdict.reason,
        media_type_detected=verdict.detected_type,
        scan_verdict=verdict.as_scan_verdict(),
        dispatch_bundle=dispatch_bundle if verdict.accepted else None,
    )

    if not verdict.accepted:
        # The reason is a stable code, never the file's content.
        logger.info(
            "inbound_scan: refused ds=%s reason=%s detected=%s trace=%s",
            datastream_id,
            verdict.reason,
            verdict.detected_type,
            trace_id,
        )
    return verdict
