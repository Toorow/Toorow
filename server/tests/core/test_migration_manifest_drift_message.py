"""The manifest-drift message has to name WHICH drift, because the remedies oppose.

WHY THIS FILE EXISTS, and it is a measured misdiagnosis rather than a hypothetical.
On 2026-08-17 `scripts/apply_migrations.py` refused to run: "migration manifest drift:
run python scripts/check_migration_catalog.py --write-manifest only for reviewed SQL".
The actual cause was ONE new migration a neighbouring session had not yet listed. The
reader instead went looking for checksum drift, measured it with a raw-byte checksum
rather than `canonical_checksum` -- which normalises line endings -- and concluded that
31 already-applied migrations had drifted on a Windows CRLF checkout. None had.

Had the offered remedy been taken on that reading, `--write-manifest` would have
re-stamped 31 applied migrations and left the manifest permanently at odds with the
production ledger, which stores the ORIGINAL checksum. That is the "an applied migration
is never re-edited" rule one level up, and the one-size message is what made it reachable.

So the two causes are named apart, and the dangerous one does NOT offer the rewrite.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from check_migration_catalog import (  # noqa: E402
    _describe_manifest_drift,
    canonical_checksum,
)

_A = {"identifier": "001", "filename": "001_a.sql", "sha256": "a" * 64}
_B = {"identifier": "002", "filename": "002_b.sql", "sha256": "b" * 64}
_EXPECTED = [_A, _B]

_WRITE_MANIFEST = "python scripts/check_migration_catalog.py --write-manifest"


def test_an_unlisted_new_migration_is_bookkeeping_and_says_so() -> None:
    """The safe case. It names the file and offers the rewrite, which is correct here."""

    message = _describe_manifest_drift([_A], _EXPECTED)

    assert "002_b.sql" in message, "the reader must not have to diff the manifest by hand"
    assert "absent from the manifest" in message
    assert _WRITE_MANIFEST in message
    assert "CHECKSUM CHANGED" not in message


def test_a_changed_checksum_refuses_to_offer_the_rewrite() -> None:
    """The dangerous case, and the whole reason this function exists.

    `--write-manifest` here would hide an edit to a migration that may already be
    applied. The message sends the reader to the ledger instead, and names the rule.
    """

    stale = [dict(_A, sha256="c" * 64), _B]
    message = _describe_manifest_drift(stale, _EXPECTED)

    assert "CHECKSUM CHANGED" in message
    assert "001_a.sql" in message
    assert "toorow_meta.schema_migrations" in message, (
        "the reader must be pointed at the ledger, which is the only thing that can say "
        "whether this migration is already applied"
    )
    assert "never re-edited" in message
    assert "would hide the change" in message


def test_a_removed_file_is_reported_as_its_own_cause() -> None:
    """A manifest entry whose file is gone is neither of the other two."""

    message = _describe_manifest_drift(_EXPECTED, [_A])

    assert "002_b.sql" in message
    assert "whose file is gone" in message
    assert "CHECKSUM CHANGED" not in message


def test_both_causes_at_once_are_both_named_and_the_rewrite_is_withheld() -> None:
    """A new file does not buy the rewrite for a changed one sitting beside it."""

    entries = [dict(_A, sha256="c" * 64)]
    expected = [_A, _B]
    message = _describe_manifest_drift(entries, expected)

    assert "absent from the manifest" in message
    assert "CHECKSUM CHANGED" in message
    assert "would hide the change" in message


def test_same_values_in_a_different_shape_is_still_reported() -> None:
    """Silence here would send the reader hunting for a difference in the VALUES.

    Order matters to the equality the caller performs, so it has to be a stated cause
    rather than the empty string.
    """

    message = _describe_manifest_drift([_B, _A], _EXPECTED)

    assert "out of order or carry unexpected keys" in message


def test_line_endings_are_not_a_cause_and_cannot_be(tmp_path: Path) -> None:
    """The claim the message relies on, asserted rather than trusted.

    `canonical_checksum` normalises CRLF, and `apply_migrations` writes the ledger with
    the SAME function -- so a Windows checkout can never produce manifest drift, and
    chasing it is wasted work. If this ever stops being true, the docstring above stops
    being true with it.
    """

    body = "-- migration\nCREATE TABLE x (id TEXT);\n"
    lf = tmp_path / "900_lf.sql"
    crlf = tmp_path / "900_crlf.sql"
    lf.write_bytes(body.encode("utf-8"))
    crlf.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))

    assert lf.read_bytes() != crlf.read_bytes(), "the fixture must really differ on disk"
    assert canonical_checksum(lf) == canonical_checksum(crlf)
