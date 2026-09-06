"""Collect and map every report a Connector declares, in process, before deploying.

Two questions per report, answered where the answers live:

  MAP     -- the intent the wizard builds, validated against the capabilities the
             module declares TODAY. This is the step that answers
             `exact_bundle_required`, `unknown_report_field`, `unsupported_grain`.
  COLLECT -- the module's own pull under `preview_capture()`, so rows are fetched
             and transformed and NOTHING is landed: no raw, no staging, no marts.

Both run against the module on disk, so a manifest edit is judged before a build.
The only thing that leaves this machine is the provider call itself, which needs a
real connection id; the platform database is read for the token and nothing else.

    python scripts/collect_and_map_locally.py <module> --connection <id> \
        --account <provider id> --from 2026-08-08 --to 2026-08-09

Without --account the pull runs unselected, which is how you SEE that a Connector
ignores the operator's choice: the two runs answer differently, or they do not.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))


def _load_environment() -> None:
    """Read .env the way the server does, without overriding what is already set."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(REPO / ".env", override=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", help="module name, e.g. the directory under server/modules")
    parser.add_argument("--connection", required=True, help="connection ref the token is minted from")
    parser.add_argument("--account", default=None, help="the provider id of the chosen account")
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--report", action="append", help="limit to these report ids")
    parser.add_argument(
        "--map-only",
        action="store_true",
        help="skip the provider calls and only judge the intents",
    )
    args = parser.parse_args()

    _load_environment()

    from core.account_topology import selected_account_pull_arguments
    from core.context_seed import load_registry_entry
    from core.datastream_intents import validate_intent
    from core.main import get_module_pull_fn
    from core.raw_landing import preview_capture
    from core.source_capabilities import normalize_capabilities

    entry = load_registry_entry(args.module)
    manifest = (entry or {}).get("manifest")
    if not isinstance(manifest, dict):
        print(f"{args.module}: no readable manifest in this checkout", file=sys.stderr)
        return 2

    # The SAME scope the compiler normalizes with: capabilities are keyed to the
    # pinned contract's connection, not to the connection the token comes from.
    # Passing the real one here answers `capability_scope_mismatch` for reasons
    # that have nothing to do with the report.
    capabilities = normalize_capabilities(
        manifest, project_id="proj_LOCAL", connection_ref_id="contract:" + args.module
    )
    account_kwargs = selected_account_pull_arguments(args.module, args.account)
    reports = {r["id"]: r for r in manifest["source_capabilities"]["reports"]}
    wanted = args.report or list(reports)

    print(f"module      : {args.module}")
    print(f"account     : {account_kwargs or 'NOT BOUND -- the pull runs unselected'}")
    print(f"window      : {args.date_from} .. {args.date_to}\n")
    print(f"{'report':24} {'map':30} {'collect':12} landed fields")
    print("-" * 118)

    failures = 0
    for report_id in wanted:
        report = reports.get(report_id)
        if report is None:
            print(f"{report_id:24} {'not declared by this module':30}")
            failures += 1
            continue
        grains = report.get("supported_grains") or [report.get("dimensions") or []]
        intent = _intent(args.module, report_id, report, list(grains[0]))
        try:
            validation = validate_intent(intent, capabilities=capabilities)
            if validation.executable:
                verdict = "compiles"
            else:
                verdict = "REFUSED " + ",".join(sorted({i.code for i in validation.issues}))
                failures += 1
        except Exception as exc:  # noqa: BLE001
            verdict, failures = f"RAISED {type(exc).__name__}", failures + 1

        collected, fields = "-", ""
        if not args.map_only:
            pull = get_module_pull_fn(args.module, report_id)
            if pull is None:
                collected = "no pull"
            else:
                try:
                    with preview_capture():
                        outcome = pull(
                            connection_id=args.connection,
                            project_id="proj_LOCAL",
                            date_from=args.date_from,
                            date_to=args.date_to,
                            pull_id="pull_LOCAL",
                            **account_kwargs,
                        )
                    count = outcome.get("row_count")
                    if count is None:
                        count = outcome.get("event_count") or 0
                    collected = f"{count} rows"
                    fields = ",".join(sorted(outcome.get("schema") or []))[:62]
                except Exception as exc:  # noqa: BLE001
                    collected = f"RAISED {type(exc).__name__}"
                    fields = str(exc)[:62]
                    failures += 1

        print(f"{report_id:24} {verdict:30} {collected:12} {fields}")

    print(f"\n{len(wanted) - failures}/{len(wanted)} reports collect and map.")
    return 1 if failures else 0


def _intent(module: str, report_id: str, report: dict, grain: list) -> dict:
    """The intent the wizard compiles, built from the report's own declaration."""
    return {
        "contract_version": "1",
        "source": {
            "kind": "connector_pull",
            "connection_ref_id": "contract:" + module,
            "report_id": report_id,
            "selection": {
                "selection_mode": report.get("selection_mode", "subset"),
                "metrics": list(report.get("metrics") or []),
                "dimensions": list(report.get("dimensions") or []),
                "grain": grain,
                "filters": [],
            },
        },
        "destination": {"policy": "managed_raw"},
        "historical": {"start": None, "end_exclusive": None},
        "schedule": {
            "mode": "daily",
            "interval_minutes": 1440,
            "timezone": "UTC",
            "timezone_origin": "scheduling_default_unconfirmed",
            "watermark": {"kind": "date_window", "delay_minutes": 0},
            "late_arrival": {"lookback_minutes": 0},
            "retry": {
                "max_attempts": 3,
                "initial_backoff_seconds": 60,
                "max_backoff_seconds": 3600,
            },
            "missed_run": {"mode": "coalesce", "max_catchup_windows": 1},
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
