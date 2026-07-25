"""Sync the canonical connector-logo set to the admin front (fail-closed).

Single source of truth for connector brand marks:
  * ``web/src/data/connector-identities.json`` — the identity/rights/checksum
    registry (id -> official asset: localPath, format, sha256, provenance).
  * ``web/public/connectors/<file>``            — the approved asset files.

The marketing front already consumes both directly. The admin front is a
DERIVED consumer: it must not hand-maintain its own divergent copy (that was the
"casserole" — 12 stale hand-inlined .svg vs the canonical .png). This script
projects the canonical set into the admin app so there is ONE source:

  1. Verifies every official asset exists and its sha256 matches the registry
     checksum (the same gate the web identity parser applies). Fail-closed: a
     missing file or checksum drift refuses the whole sync.
  2. Copies each referenced asset byte-identically into
     ``ui/admin/public/connectors/`` and prunes any admin /connectors file that
     is NOT in the canonical set.
  3. Writes ``ui/admin/src/generated/connector-logos.json`` — a lean resolver
     map (slug -> /connectors/<file>) keyed by BOTH the identity id (e.g.
     ``meta-ads``) and the file stem (e.g. ``meta``) so a caller can pass either
     the module id (real data) or a provider slug (mockup literal) and still
     resolve the right filename + extension. ``ConnectorLogo`` reads this map.

``--check`` verifies the admin front is in lockstep without writing (the build /
CI gate, mirroring ``export_connector_registry.py --check``). ``--write``
applies. Creating a connector registers its logo once in the canonical source
(add-connector skill) and re-runs this — the admin never diverges again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
IDENTITIES = ROOT / "web" / "src" / "data" / "connector-identities.json"
CANONICAL_DIR = ROOT / "web" / "public" / "connectors"
ADMIN_PUBLIC_DIR = ROOT / "ui" / "admin" / "public" / "connectors"
ADMIN_MAP = ROOT / "ui" / "admin" / "src" / "generated" / "connector-logos.json"

CHECKSUM_PREFIX = "sha256:"


class LogoSyncError(ValueError):
    """Raised when the canonical logo set cannot produce an honest admin mirror."""


def _sha256(path: Path) -> str:
    return CHECKSUM_PREFIX + hashlib.sha256(path.read_bytes()).hexdigest()


def _slug(value: str) -> str:
    return value.strip().lower().replace(" ", "-")


def _load_identities() -> dict[str, Any]:
    try:
        contract = json.loads(IDENTITIES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LogoSyncError(f"connector-identities.json: {exc}") from exc
    identities = contract.get("identities")
    if not isinstance(identities, dict) or not identities:
        raise LogoSyncError("connector-identities.json: $.identities must be a non-empty object")
    return identities


def _canonical_plan() -> tuple[dict[str, str], dict[str, str]]:
    """Return (files, resolver_map).

    ``files``        : filename -> absolute canonical source path (deduped).
    ``resolver_map`` : slug -> "/connectors/<file>" keyed by identity id AND stem.

    Fail-closed: every official asset must exist and its sha256 must match the
    registry checksum. Fallback identities (no approved asset) are skipped for
    file copy but still resolve to the shared generic mark.
    """
    identities = _load_identities()
    files: dict[str, str] = {}
    resolver: dict[str, str] = {}

    for connector_id in sorted(identities):
        entry = identities[connector_id]
        asset = entry.get("asset") if isinstance(entry, dict) else None
        if not isinstance(asset, dict):
            raise LogoSyncError(f"{connector_id}: $.asset must be an object")

        if asset.get("kind") == "fallback":
            resolver[_slug(connector_id)] = "/connectors/generic.svg"
            continue

        local_path = asset.get("localPath")
        checksum = asset.get("checksum")
        if not isinstance(local_path, str) or not local_path.startswith("/connectors/"):
            raise LogoSyncError(f"{connector_id}: $.asset.localPath must be a /connectors/ path")
        filename = local_path[len("/connectors/") :]
        if "/" in filename or ".." in filename or "\\" in filename:
            raise LogoSyncError(f"{connector_id}: $.asset.localPath is not a safe filename")

        source = CANONICAL_DIR / filename
        if not source.is_file():
            raise LogoSyncError(f"{connector_id}: approved asset missing: web/public{local_path}")
        actual = _sha256(source)
        if not isinstance(checksum, str) or checksum != actual:
            raise LogoSyncError(
                f"{connector_id}: checksum drift for {local_path}: "
                f"registry {checksum!r} != file {actual!r}"
            )

        files[filename] = str(source)
        # Resolve by identity id (real data: module_name) AND file stem (mockup
        # literal / provider slug). Aliased ids (meta-ads -> meta.svg) resolve both.
        resolver[_slug(connector_id)] = local_path
        resolver.setdefault(_slug(Path(filename).stem), local_path)

    if "generic.svg" not in files:
        generic = CANONICAL_DIR / "generic.svg"
        if not generic.is_file():
            raise LogoSyncError("shared fallback missing: web/public/connectors/generic.svg")
        files["generic.svg"] = str(generic)

    return files, resolver


def _serialize_map(resolver: dict[str, str]) -> bytes:
    payload = {
        "contractVersion": "1",
        "_note": "GENERATED by scripts/sync_connector_logos.py from web/src/data/connector-identities.json. Do not edit by hand.",
        "logos": {slug: resolver[slug] for slug in sorted(resolver)},
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _drift(files: dict[str, str], resolver: dict[str, str]) -> list[str]:
    """Return human-readable drift lines; empty means the admin mirror is current."""
    problems: list[str] = []

    expected = set(files)
    present = {p.name for p in ADMIN_PUBLIC_DIR.glob("*")} if ADMIN_PUBLIC_DIR.is_dir() else set()
    for stale in sorted(present - expected):
        problems.append(f"stale admin asset (not in canonical set): connectors/{stale}")
    for name in sorted(expected):
        dest = ADMIN_PUBLIC_DIR / name
        if not dest.is_file():
            problems.append(f"missing admin asset: connectors/{name}")
        elif dest.read_bytes() != Path(files[name]).read_bytes():
            problems.append(f"admin asset differs from canonical: connectors/{name}")

    expected_map = _serialize_map(resolver)
    if not ADMIN_MAP.is_file():
        problems.append(f"missing generated map: {ADMIN_MAP.relative_to(ROOT)}")
    elif ADMIN_MAP.read_bytes() != expected_map:
        problems.append(f"stale generated map: {ADMIN_MAP.relative_to(ROOT)}")
    return problems


def _apply(files: dict[str, str], resolver: dict[str, str]) -> list[str]:
    """Write the admin mirror; return the list of actions taken."""
    actions: list[str] = []
    ADMIN_PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

    expected = set(files)
    present = {p.name for p in ADMIN_PUBLIC_DIR.glob("*") if p.is_file()}
    for stale in sorted(present - expected):
        (ADMIN_PUBLIC_DIR / stale).unlink()
        actions.append(f"removed connectors/{stale}")
    for name in sorted(expected):
        dest = ADMIN_PUBLIC_DIR / name
        src_bytes = Path(files[name]).read_bytes()
        if not dest.is_file() or dest.read_bytes() != src_bytes:
            dest.write_bytes(src_bytes)
            actions.append(f"wrote connectors/{name}")

    expected_map = _serialize_map(resolver)
    ADMIN_MAP.parent.mkdir(parents=True, exist_ok=True)
    if not ADMIN_MAP.is_file() or ADMIN_MAP.read_bytes() != expected_map:
        ADMIN_MAP.write_bytes(expected_map)
        actions.append(f"wrote {ADMIN_MAP.relative_to(ROOT)}")
    return actions


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Sync the admin mirror in place")
    mode.add_argument("--check", action="store_true", help="Fail if the admin mirror is stale")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        files, resolver = _canonical_plan()
    except LogoSyncError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.check:
        problems = _drift(files, resolver)
        if problems:
            print("Admin connector-logo mirror is stale:", file=sys.stderr)
            for line in problems:
                print(f"  - {line}", file=sys.stderr)
            print("Run: uv run python scripts/sync_connector_logos.py --write", file=sys.stderr)
            return 1
        print(f"Admin connector-logo mirror is in sync ({len(files)} assets).")
        return 0

    actions = _apply(files, resolver)
    if actions:
        for line in actions:
            print(line)
        print(f"Synced {len(files)} canonical assets to the admin front.")
    else:
        print(f"Admin connector-logo mirror already current ({len(files)} assets).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
