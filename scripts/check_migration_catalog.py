"""Fail fast when SQL migration identifiers are ambiguous or discontinuous."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

MIGRATION_NAME = re.compile(r"^(?P<identifier>\d{3})_[a-z0-9][a-z0-9_]*\.sql$")
PINNED_MIGRATIONS = {109: "109_entry_invitation_without_org.sql"}


class MigrationCatalogError(ValueError):
    """The migration directory cannot be applied in one safe numeric order."""


def canonical_checksum(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_manifest(directory: Path, files: list[Path], errors: list[str]) -> None:
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        errors.append("migration manifest missing: manifest.json")
        return
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = document["migrations"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        errors.append(f"invalid migration manifest: {type(exc).__name__}")
        return
    expected = [
        {
            "identifier": path.name[:3],
            "filename": path.name,
            "sha256": canonical_checksum(path),
        }
        for path in files
        if MIGRATION_NAME.fullmatch(path.name)
    ]
    if entries != expected:
        errors.append(_describe_manifest_drift(entries, expected))


def _describe_manifest_drift(entries: list, expected: list[dict]) -> str:
    """Say WHICH drift this is, because the two have opposite remedies.

    THE MESSAGE USED TO BE ONE SENTENCE for both cases -- "run --write-manifest only
    for reviewed SQL" -- and that cost a real misdiagnosis on 2026-08-17: a NEW,
    unlisted migration reported the same words as a CHANGED one, the reader looked for
    checksum drift, measured it with a raw-byte checksum instead of
    :func:`canonical_checksum`, and concluded that 31 already-applied migrations had
    drifted. None had. `--write-manifest` would then have re-stamped 31 applied
    migrations and left the manifest permanently at odds with the production ledger,
    which stores the ORIGINAL checksum -- the "never re-edit an applied migration" rule,
    one level up.

    So the two are named apart:

    * an unlisted or removed FILE is bookkeeping. `--write-manifest` is exactly right.
    * a CHANGED checksum on a file already in the manifest is the dangerous one. It may
      be a legitimate pre-application edit, and it may be an edit to something already
      applied somewhere -- and only the ledger can tell which, so the message sends the
      reader there first instead of offering the rewrite.

    Line endings are NOT a cause and cannot be: :func:`canonical_checksum` normalises
    them, and ``apply_migrations`` writes the ledger with the same function. A CRLF
    working tree on Windows is invisible to both, which is why chasing it is wasted work.
    """

    listed = {entry.get("filename") for entry in entries if isinstance(entry, dict)}
    on_disk = {entry["filename"] for entry in expected}
    unlisted = sorted(on_disk - listed)
    removed = sorted(listed - on_disk)

    by_name = {entry["filename"]: entry["sha256"] for entry in expected}
    changed = sorted(
        entry["filename"]
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("filename") in by_name
        and entry.get("sha256") != by_name[entry["filename"]]
    )

    parts: list[str] = []
    if unlisted:
        parts.append(f"migration(s) on disk and absent from the manifest: {unlisted}")
    if removed:
        parts.append(f"manifest entry(ies) whose file is gone: {removed}")
    if changed:
        parts.append(
            f"CHECKSUM CHANGED on already-listed migration(s): {changed} -- do NOT "
            "re-stamp these before checking the ledger "
            "(SELECT identifier, checksum FROM toorow_meta.schema_migrations). An "
            "applied migration is never re-edited; correct it with the next one"
        )
    if not parts:
        # Same names and same checksums, different ORDER or shape. Worth saying so:
        # the reader would otherwise hunt for a difference that is not in the values.
        parts.append("manifest entries are out of order or carry unexpected keys")

    remedy = (
        "run python scripts/check_migration_catalog.py --write-manifest"
        if not changed
        else "resolve the changed checksum(s) first; --write-manifest would hide the change"
    )
    return "migration manifest drift: " + "; ".join(parts) + f". Then {remedy}."


def validate_catalog(directory: Path, *, verify_manifest: bool = False) -> list[Path]:
    """Return migrations in order, or raise with all catalog errors."""
    if not directory.is_dir():
        raise MigrationCatalogError(f"migration directory not found: {directory}")

    files = sorted(directory.glob("*.sql"), key=lambda path: path.name)
    errors: list[str] = []
    by_identifier: dict[int, list[Path]] = defaultdict(list)

    for path in files:
        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            errors.append(f"invalid filename: {path.name} (expected NNN_description.sql)")
            continue
        by_identifier[int(match.group("identifier"))].append(path)

    for identifier, paths in sorted(by_identifier.items()):
        if len(paths) > 1:
            names = ", ".join(path.name for path in paths)
            errors.append(f"duplicate migration {identifier:03d}: {names}")

    for identifier, expected_name in PINNED_MIGRATIONS.items():
        paths = by_identifier.get(identifier)
        if paths and len(paths) == 1 and paths[0].name != expected_name:
            errors.append(
                f"pinned migration {identifier:03d} must be {expected_name}; "
                f"found {paths[0].name}"
            )

    if by_identifier:
        highest = max(by_identifier)
        missing = [
            identifier
            for identifier in range(1, highest + 1)
            if identifier not in by_identifier
        ]
        if missing:
            rendered = ", ".join(f"{identifier:03d}" for identifier in missing)
            errors.append(f"missing migration identifiers: {rendered}")
    else:
        errors.append("no migrations found")

    ordered = [by_identifier[identifier][0] for identifier in sorted(by_identifier)]
    if verify_manifest:
        _validate_manifest(directory, ordered, errors)

    if errors:
        raise MigrationCatalogError("\n".join(errors))

    return ordered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path("infra/nango/migrations"),
        help="migration directory (default: infra/nango/migrations)",
    )
    parser.add_argument("--write-manifest", action="store_true")
    args = parser.parse_args(argv)

    if args.write_manifest:
        migrations = validate_catalog(args.directory)
        document = {
            "version": 1,
            "migrations": [
                {
                    "identifier": path.name[:3],
                    "filename": path.name,
                    "sha256": canonical_checksum(path),
                }
                for path in migrations
            ],
        }
        (args.directory / "manifest.json").write_text(
            json.dumps(document, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote migration manifest: {len(migrations)} entries")
        return 0

    try:
        migrations = validate_catalog(args.directory, verify_manifest=True)
    except MigrationCatalogError as exc:
        print(f"migration catalog invalid:\n{exc}", file=sys.stderr)
        return 1

    print(
        f"migration catalog OK: {len(migrations)} migrations "
        f"({migrations[0].stem[:3]}..{migrations[-1].stem[:3]})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
