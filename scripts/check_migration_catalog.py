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
        errors.append(
            "migration manifest drift: run "
            "python scripts/check_migration_catalog.py --write-manifest only for reviewed SQL"
        )


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
