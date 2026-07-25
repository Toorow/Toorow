"""Build api_catalog.json for a connector module from cached local source files.

Usage:
    uv run python scripts/build_api_catalog.py \\
        --module <name> \\
        --sources-dir <dir> \\
        [--out <path>] \\
        [--dry-run] \\
        [--report <path>]

NO network calls are made. All source files must be pre-fetched locally
(see the module's catalog_sources.json for the curl commands).

Output defaults to server/modules/<name>/api_catalog.json.
--dry-run prints the fusion report without writing the catalog.
--report <path> writes the fusion report JSON to a file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# --- sys.path bootstrap (same pattern as export_connector_registry.py) ---
_ROOT = Path(__file__).resolve().parents[1]
_SERVER_DIR = _ROOT / "server"
_SCRIPTS_DIR = _ROOT / "scripts"

for _p in (_SERVER_DIR, _SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from catalog_gen.supermetrics_parser import parse_supermetrics_markdown, SupermetricsEntry  # noqa: E402
from catalog_gen.discovery_parser import parse_discovery_document, DiscoveryEntry  # noqa: E402
from catalog_gen.merge import (  # noqa: E402
    OfficialField,
    EnrichmentField,
    merge,
    serialize_catalog,
)


class CatalogGenError(ValueError):
    """Raised when catalog generation cannot produce an honest output."""


def _load_json(path: Path, label: str) -> dict:
    if not path.exists():
        raise CatalogGenError(f"{label}: file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogGenError(f"{label}: invalid JSON at line {exc.lineno}: {path}") from exc


def _read_text(path: Path, label: str) -> str:
    if not path.exists():
        raise CatalogGenError(f"{label}: file not found: {path}")
    return path.read_text(encoding="utf-8")


def _load_official_fields(
    source_cfg: dict,
    sources_dir: Path,
) -> list[OfficialField]:
    """Load official fields from the source config's official source file."""
    kind = source_cfg.get("kind", "")
    filename = source_cfg.get("file", "")
    file_path = sources_dir / filename if filename else None

    if kind != "official" or file_path is None:
        return []

    parser_type = source_cfg.get("parser", "official_fields_json")

    if parser_type == "official_fields_json":
        # Hand-maintained official_fields.json: list of
        # {field_id, kind, data_type?, description?, section?, source_field?}.
        # source_field passthrough (25.4 AC2): defaults to field_id in OfficialField
        # when absent; distinct only when the provider token differs (e.g. Meta
        # date -> date_start) so the manifest<->catalog gate stays green.
        data = _load_json(file_path, f"official_fields_json:{filename}")
        fields = data if isinstance(data, list) else data.get("fields", [])
        return [
            OfficialField(
                field_id=f["field_id"],
                kind=f["kind"],
                data_type=f.get("data_type", "string"),
                description=f.get("description"),
                section=f.get("section", "UNKNOWN"),
                source_field=f.get("source_field"),
            )
            for f in fields
            if f.get("field_id") and f.get("kind")
        ]

    if parser_type == "discovery_json":
        doc = _load_json(file_path, f"discovery_json:{filename}")
        configs = source_cfg.get("discovery_configs", [])
        all_entries: list[DiscoveryEntry] = []
        for cfg in configs:
            all_entries.extend(parse_discovery_document(doc, cfg))
        return [
            OfficialField(
                field_id=e.field_id,
                kind=e.kind,
                data_type="string",
                description=e.description,
                section=source_cfg.get("default_section", "UNKNOWN"),
            )
            for e in all_entries
        ]

    raise CatalogGenError(f"Unknown official parser type: {parser_type!r}")


def _load_enrichment_fields(
    source_cfg: dict,
    sources_dir: Path,
) -> list[EnrichmentField]:
    """Load enrichment fields from an enrichment source config."""
    kind = source_cfg.get("kind", "")
    if kind != "enrichment":
        return []

    filename = source_cfg.get("file", "")
    if not filename:
        return []

    file_path = sources_dir / filename
    parser_type = source_cfg.get("parser", "supermetrics_markdown")

    if parser_type == "supermetrics_markdown":
        text = _read_text(file_path, f"supermetrics_markdown:{filename}")
        sm_entries = parse_supermetrics_markdown(text)
        return [
            EnrichmentField(
                field_id=e.field_id,
                kind=e.kind,
                data_type=e.data_type,
                description=e.description,
                section=e.section,
                deprecated=e.deprecated,
            )
            for e in sm_entries
        ]

    raise CatalogGenError(f"Unknown enrichment parser type: {parser_type!r}")


def build_catalog(
    module: str,
    sources_dir: Path,
    out_path: Path,
    dry_run: bool = False,
    report_path: Path | None = None,
) -> None:
    """Full generation pipeline for one module."""

    # Load catalog_sources.json from sources_dir (the module config)
    catalog_sources_path = sources_dir / "catalog_sources.json"
    cfg = _load_json(catalog_sources_path, "catalog_sources.json")

    # Validate required config keys
    for key in ("connector", "api_version", "generated_at", "sources"):
        if key not in cfg:
            raise CatalogGenError(f"catalog_sources.json missing required key: {key!r}")

    if not any(s.get("kind") == "official" for s in cfg.get("sources", [])):
        raise CatalogGenError("catalog_sources.json: at least one source with kind='official' required")

    # Load manifest for exposure derivation
    manifest_path = _ROOT / "server" / "modules" / module / "manifest.json"
    manifest: dict = {}
    if manifest_path.exists():
        manifest = _load_json(manifest_path, f"manifest.json:{module}")

    # Parse all source files
    official_fields: list[OfficialField] = []
    enrichment_fields: list[EnrichmentField] = []

    for source_cfg in cfg.get("sources", []):
        if source_cfg.get("kind") == "official":
            official_fields.extend(_load_official_fields(source_cfg, sources_dir))
        elif source_cfg.get("kind") == "enrichment":
            enrichment_fields.extend(_load_enrichment_fields(source_cfg, sources_dir))

    if not official_fields:
        raise CatalogGenError(
            "No official fields loaded. Check your catalog_sources.json source configs."
        )

    # Merge
    catalog, report = merge(official_fields, enrichment_fields, manifest, cfg)

    # Print fusion report
    report_dict = report.as_dict()
    print("=== Fusion Report ===")
    print(json.dumps(report_dict, indent=2))
    print("=== end ===")

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report_dict, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"Report written to: {report_path}")

    if dry_run:
        print("[dry-run] Catalog NOT written.")
        return

    # Write catalog
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(serialize_catalog(catalog), encoding="utf-8", newline="\n")
    print(f"Catalog written to: {out_path}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--module",
        required=True,
        help="Module name (e.g. facebook-ads). Used to locate manifest.json and default output path.",
    )
    parser.add_argument(
        "--sources-dir",
        required=True,
        type=Path,
        help="Directory containing cached source files + catalog_sources.json.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output path for api_catalog.json (default: server/modules/<name>/api_catalog.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print fusion report without writing the catalog.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write fusion report JSON to this path.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_path = args.out or (
        _ROOT / "server" / "modules" / args.module / "api_catalog.json"
    )
    try:
        build_catalog(
            module=args.module,
            sources_dir=args.sources_dir,
            out_path=out_path,
            dry_run=args.dry_run,
            report_path=args.report,
        )
        return 0
    except CatalogGenError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
