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
import re
import struct
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
IDENTITIES = ROOT / "web" / "src" / "data" / "connector-identities.json"
CANONICAL_DIR = ROOT / "web" / "public" / "connectors"
ADMIN_PUBLIC_DIR = ROOT / "ui" / "admin" / "public" / "connectors"
ADMIN_MAP = ROOT / "ui" / "admin" / "src" / "generated" / "connector-logos.json"

CHECKSUM_PREFIX = "sha256:"

# A checksum only proves "these bytes did not change" — it happily blesses an ICO,
# a JPEG or an HTML error page saved under a .png name, and that is exactly how the
# set rotted (2026-07-25: 4 renamed files, 6 favicons of 16-32px, 5 connectors
# silently sharing one image). The gates below verify what the bytes ARE.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MIN_RASTER_PX = 64

# Frozen debt, not a loophole: brands whose own site serves nothing bigger today.
# A ratchet — each asset may only get better. Raise the recorded size when it does,
# never add an entry to make a fresh scrape pass.
LOW_RES_DEBT = {"adjust.png": 48, "amazon-ads.png": 32, "ias.png": 32}

# Same-brand marks that legitimately reuse one file. Any OTHER pair of connectors
# resolving to identical bytes is the "everything shows the Google G" bug.
SHARED_MARKS = {
    frozenset({"amazon-ads", "amazon-dsp"}),
    frozenset({"linkedin-ads", "linkedin-company-pages"}),
}

# Front-end sources allowed to name a /connectors/ file literally. Renaming an asset
# must not silently 404 a hand-written src=/url() — so those literals are checked too.
LITERAL_ROOTS = (ROOT / "web" / "src", ROOT / "ui" / "admin" / "src")
LITERAL_SUFFIXES = (".ts", ".tsx", ".astro", ".css")
LITERAL_RE = re.compile(r"/connectors/([A-Za-z0-9._-]+\.(?:svg|png))")
# Fixtures and doc comments that name a deliberately non-existent asset.
LITERAL_IGNORE = {"example.svg", "logo.svg", "xxx.svg"}


class LogoSyncError(ValueError):
    """Raised when the canonical logo set cannot produce an honest admin mirror."""


def _sha256(path: Path) -> str:
    return CHECKSUM_PREFIX + hashlib.sha256(path.read_bytes()).hexdigest()


def _slug(value: str) -> str:
    return value.strip().lower().replace(" ", "-")


def _validate_asset(connector_id: str, filename: str, source: Path) -> None:
    """Fail-closed on bytes that are not the brand mark the registry claims."""
    payload = source.read_bytes()
    suffix = source.suffix.lower()

    if suffix == ".png":
        if payload[:8] != PNG_MAGIC:
            raise LogoSyncError(
                f"{connector_id}: {filename} is named .png but its bytes are not a PNG "
                f"(starts with {payload[:4].hex()}) — a scrape saved the wrong file"
            )
        width, height = struct.unpack(">II", payload[16:24])
        floor = LOW_RES_DEBT.get(filename, MIN_RASTER_PX)
        if min(width, height) < floor:
            hint = (
                f"recorded debt is {floor}px, assets may only improve"
                if filename in LOW_RES_DEBT
                else f"minimum is {MIN_RASTER_PX}px — a favicon is not a brand mark"
            )
            raise LogoSyncError(f"{connector_id}: {filename} is {width}x{height}: {hint}")
    elif suffix == ".svg":
        head = payload.lstrip()[:512].lower()
        if not head.startswith((b"<svg", b"<?xml")):
            raise LogoSyncError(
                f"{connector_id}: {filename} is named .svg but is not SVG markup "
                f"(starts with {payload[:24]!r}) — a scrape saved an error page"
            )
    else:
        raise LogoSyncError(f"{connector_id}: unsupported asset format {suffix!r} for {filename}")


def _assert_no_accidental_sharing(owners: dict[str, list[str]]) -> None:
    """Two connectors resolving to identical bytes must be a declared brand family."""
    for digest, connector_ids in sorted(owners.items()):
        if len(connector_ids) < 2:
            continue
        if frozenset(connector_ids) in SHARED_MARKS:
            continue
        joined = ", ".join(sorted(connector_ids))
        raise LogoSyncError(
            f"identical logo bytes ({digest[:19]}...) shared by: {joined}. "
            "Give each connector its own official mark, or declare the pair in SHARED_MARKS."
        )


def _assert_literals_resolve(files: dict[str, str]) -> None:
    """Every hand-written /connectors/<file> in the fronts must exist in the set."""
    dangling: list[str] = []
    for root in LITERAL_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in LITERAL_SUFFIXES or not path.is_file():
                continue
            for name in set(LITERAL_RE.findall(path.read_text(encoding="utf-8", errors="ignore"))):
                if name in LITERAL_IGNORE or name in files:
                    continue
                dangling.append(f"{path.relative_to(ROOT).as_posix()} -> /connectors/{name}")
    if dangling:
        listing = "\n  - ".join(sorted(dangling))
        raise LogoSyncError(
            "hand-written logo paths point at assets outside the canonical set "
            f"(they would 404 in the browser):\n  - {listing}"
        )


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
    owners: dict[str, list[str]] = {}

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

        _validate_asset(connector_id, filename, source)
        owners.setdefault(actual, []).append(connector_id)

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

    _assert_no_accidental_sharing(owners)
    _assert_literals_resolve(files)

    orphans = sorted(p.name for p in CANONICAL_DIR.glob("*") if p.is_file() and p.name not in files)
    if orphans:
        raise LogoSyncError(
            "web/public/connectors holds files no connector claims "
            f"(stale scrapes drift back in from there): {', '.join(orphans)}"
        )

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
