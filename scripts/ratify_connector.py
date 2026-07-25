"""Live ratification probe harness for a connector module (Story 25.6).

LOCAL-ONLY, HUMAN-GATED (AI-08). This tool is NEVER run in CI: it issues real,
minimal API requests against a REAL connected account to verify that a
connector's DECLARED contract (its api_catalog fields, its error_map, its
account_topology) matches what the provider actually returns. The output is a
deterministic, fully redacted JSON evidence report committed under
``server/modules/<name>/reports/ratification-<probed_at>.json``. That report is
the ONLY artifact that lifts ``public_catalog.verification: blocked`` to a
"verified" readiness state in the public registry (see
scripts/export_connector_registry.py).

Usage:
    uv run python scripts/ratify_connector.py \\
        --module <name> \\
        --connection <connection_ref_id> \\
        [--tier core|standard|advanced|all] \\
        [--probe-auth] \\
        [--account <account_id>] \\
        [--probed-at <ISO8601>] \\
        [--out <path>]

Design (AD-2 / AD-3 / AI-08):
  * The harness is provider-agnostic. It reads the ``probe`` block from the
    module's ``catalog_sources/catalog_sources.json`` (request_style + batch_size)
    and implements a SMALL set of request styles generically. No provider names
    or provider field lists live in this file -- they come from the catalog.
  * request_style == "none" (generic / google-sheets / github): the catalog IS
    the contract by construction. The harness records a STRUCTURAL ratification
    (every exposed/planned field -> ok) with an explicit structural note; no live
    request is issued.
  * AD-3: tokens are obtained immediately before use and NEVER written to the
    report. The report carries only a redacted connection id and the account id.
  * Determinism: field ordering is sorted; ``probed_at`` comes from --probed-at
    or the real clock AT RUN TIME (a local tool -- clock use is allowed in the
    runner; test fixtures pin it).
  * Quota: batches respect ``batch_size``; a RateLimitError pauses and resumes
    (breaker-aware) via an injectable sleep, so a rate-limited provider never
    loses probe progress.

The HTTP layer is injected (``probe_request`` callable) so the whole harness is
unit-testable WITHOUT network or live credentials (Story 25.6 AC4).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# --- sys.path bootstrap (same pattern as build_api_catalog.py) ---
_ROOT = Path(__file__).resolve().parents[1]
_SERVER_DIR = _ROOT / "server"
_SCRIPTS_DIR = _ROOT / "scripts"
for _p in (_SERVER_DIR, _SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ---------------------------------------------------------------------------
# Constants: the field-probe status vocabulary + verdict vocabulary.
# ---------------------------------------------------------------------------

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_UNSUPPORTED = "unsupported"
STATUS_UNPROBED = "unprobed"
# A rejected field carries its error_class inline: "rejected(<error_class>)".
STATUS_REJECTED_PREFIX = "rejected"

VERDICT_RATIFIED = "ratified"
VERDICT_PARTIAL = "partial"
VERDICT_FAILED = "failed"

# The declared request styles (AC2). "none" == structural ratification.
KNOWN_REQUEST_STYLES = frozenset(
    {
        "fields_param",
        "metrics_dimensions",
        "dimensions_only",
        "report_statistics",
        "none",
    }
)

VALID_TIERS = ("core", "standard", "advanced")

# Exposures that participate in ratification (planned fields are declared but not
# yet extracted -- the probe still verifies they exist against the live API).
PROBED_EXPOSURES = frozenset({"exposed", "planned"})


class RatificationError(ValueError):
    """Raised when the harness cannot produce an honest ratification report."""


def rejected_status(error_class: str) -> str:
    """Return the canonical ``rejected(<error_class>)`` status string."""
    return f"{STATUS_REJECTED_PREFIX}({error_class})"


# ---------------------------------------------------------------------------
# ProbeResult: the small, provider-neutral result of a single field request.
# The injected ``probe_request`` callable returns one of these per batch.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeOutcome:
    """Outcome of probing a batch of fields against the live API.

    Attributes:
        present: field_ids the response actually returned a value for -> ``ok``.
        empty: field_ids accepted by the API but with no value in the window.
        rejected: {field_id: error_class} for fields the API refused.
        unsupported: field_ids the API does not recognise at all.
        rate_limited: True if the provider returned 429 for this batch (retry).
        retry_after: seconds hint from the provider (None if absent).
    """

    present: frozenset[str] = frozenset()
    empty: frozenset[str] = frozenset()
    rejected: dict[str, str] = field(default_factory=dict)
    unsupported: frozenset[str] = frozenset()
    rate_limited: bool = False
    retry_after: int | None = None


# The injected request callable. It receives the module name, the request_style,
# the account id, the token (or None in structural/dry probes), and the batch of
# {field_id, source_field, kind} dicts; it returns a ProbeOutcome.
ProbeRequest = Callable[..., ProbeOutcome]


# ---------------------------------------------------------------------------
# Catalog + probe-config loading.
# ---------------------------------------------------------------------------


def _module_dir(module: str, modules_dir: Path) -> Path:
    return modules_dir / module


def load_probe_config(module: str, modules_dir: Path) -> dict[str, Any]:
    """Read the ``probe`` block from the module's catalog_sources.json (AC2)."""
    path = (
        _module_dir(module, modules_dir)
        / "catalog_sources"
        / "catalog_sources.json"
    )
    if not path.exists():
        raise RatificationError(f"{module}: catalog_sources.json not found: {path}")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    probe = cfg.get("probe")
    if not isinstance(probe, dict):
        raise RatificationError(
            f"{module}: catalog_sources.json is missing the required 'probe' block"
        )
    style = probe.get("request_style")
    if style not in KNOWN_REQUEST_STYLES:
        raise RatificationError(
            f"{module}: probe.request_style {style!r} is not one of "
            f"{sorted(KNOWN_REQUEST_STYLES)}"
        )
    batch_size = probe.get("batch_size", 0)
    if not isinstance(batch_size, int) or batch_size < 0:
        raise RatificationError(
            f"{module}: probe.batch_size must be a non-negative integer"
        )
    return probe


def load_catalog_fields(
    module: str,
    modules_dir: Path,
    tiers: tuple[str, ...],
) -> list[dict[str, str]]:
    """Return the sorted, dedup'd probe targets for the selected tier(s).

    A probe target is ``{field_id, source_field, kind, tier}`` for every
    api_catalog field of a selected tier whose exposure is exposed|planned.
    Sorted by (kind, field_id) for deterministic batching.
    """
    path = _module_dir(module, modules_dir) / "api_catalog.json"
    if not path.exists():
        raise RatificationError(f"{module}: api_catalog.json not found: {path}")
    catalog = json.loads(path.read_text(encoding="utf-8"))
    fields = catalog.get("fields")
    if not isinstance(fields, list):
        raise RatificationError(f"{module}: api_catalog.json has no 'fields' array")

    targets: list[dict[str, str]] = []
    for f in fields:
        if f.get("exposure") not in PROBED_EXPOSURES:
            continue
        if f.get("tier") not in tiers:
            continue
        field_id = f.get("field_id")
        if not field_id:
            continue
        targets.append(
            {
                "field_id": field_id,
                "source_field": f.get("source_field", field_id),
                "kind": f.get("kind", "metric"),
                "tier": f.get("tier", ""),
            }
        )
    targets.sort(key=lambda t: (t["kind"], t["field_id"]))
    return targets


def load_api_version(module: str, modules_dir: Path) -> str:
    path = _module_dir(module, modules_dir) / "api_catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    return catalog.get("api_version", "")


def has_account_topology(module: str, modules_dir: Path) -> bool:
    path = _module_dir(module, modules_dir) / "manifest.json"
    if not path.exists():
        return False
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return "account_topology" in manifest


# ---------------------------------------------------------------------------
# Batching helper (respects batch_size; batch_size 0/1 falls back to whole/one).
# ---------------------------------------------------------------------------


def batched(targets: list[dict[str, str]], batch_size: int) -> list[list[dict[str, str]]]:
    """Split *targets* into batches of at most *batch_size* (0 -> one batch)."""
    if batch_size <= 0:
        return [list(targets)] if targets else []
    return [targets[i : i + batch_size] for i in range(0, len(targets), batch_size)]


# ---------------------------------------------------------------------------
# Field probe (AC1): classify every field of the selected tier(s).
# ---------------------------------------------------------------------------


def probe_fields(
    module: str,
    request_style: str,
    batch_size: int,
    account: str,
    targets: list[dict[str, str]],
    probe_request: ProbeRequest,
    *,
    token: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_retries: int = 5,
) -> dict[str, str]:
    """Run the field probe and return {field_id: status} for every target.

    Batches by *batch_size*. On a rate-limited batch, pauses (breaker-aware) via
    *sleep* and retries the SAME batch, so no probe progress is lost (AC1). A
    batch that comes back with any ``rejected`` or ``unsupported`` fields is
    RE-PROBED field-by-field so the exact offending field is captured rather than
    tainting the whole batch (surgical precision, AC1/AC4).
    """
    statuses: dict[str, str] = {}
    for batch in batched(targets, batch_size):
        _probe_one_batch(
            module,
            request_style,
            account,
            batch,
            probe_request,
            statuses,
            token=token,
            sleep=sleep,
            max_retries=max_retries,
            allow_bisect=len(batch) > 1,
        )
    return statuses


def _probe_one_batch(
    module: str,
    request_style: str,
    account: str,
    batch: list[dict[str, str]],
    probe_request: ProbeRequest,
    statuses: dict[str, str],
    *,
    token: str | None,
    sleep: Callable[[float], None],
    max_retries: int,
    allow_bisect: bool,
) -> None:
    outcome = _request_with_retry(
        module,
        request_style,
        account,
        batch,
        probe_request,
        token=token,
        sleep=sleep,
        max_retries=max_retries,
    )

    if outcome is None:
        # The batch was rate-limited past max_retries: leave every field UNPROBED
        # (absent from statuses) so coverage.unprobed surfaces the gap honestly.
        return

    # If a multi-field batch produced ambiguity (rejected/unsupported present),
    # re-probe field-by-field to attribute the outcome to the exact field.
    if allow_bisect and (outcome.rejected or outcome.unsupported):
        for target in batch:
            _probe_one_batch(
                module,
                request_style,
                account,
                [target],
                probe_request,
                statuses,
                token=token,
                sleep=sleep,
                max_retries=max_retries,
                allow_bisect=False,
            )
        return

    for target in batch:
        statuses[target["field_id"]] = _classify_target(target["field_id"], outcome)


def _classify_target(field_id: str, outcome: ProbeOutcome) -> str:
    if field_id in outcome.rejected:
        return rejected_status(outcome.rejected[field_id])
    if field_id in outcome.unsupported:
        return STATUS_UNSUPPORTED
    if field_id in outcome.present:
        return STATUS_OK
    if field_id in outcome.empty:
        return STATUS_EMPTY
    # The API accepted the batch but said nothing about this field -> empty.
    return STATUS_EMPTY


def _request_with_retry(
    module: str,
    request_style: str,
    account: str,
    batch: list[dict[str, str]],
    probe_request: ProbeRequest,
    *,
    token: str | None,
    sleep: Callable[[float], None],
    max_retries: int,
) -> ProbeOutcome | None:
    """Issue one batch request, pausing + retrying on rate limit (breaker-aware).

    Returns the resolved outcome, or ``None`` if the provider kept rate-limiting
    past *max_retries* (the caller then leaves those fields unprobed).
    """
    attempts = 0
    while True:
        outcome = probe_request(
            module=module,
            request_style=request_style,
            account=account,
            batch=batch,
            token=token,
        )
        if not outcome.rate_limited:
            return outcome
        attempts += 1
        if attempts > max_retries:
            return None
        # Breaker-aware pause: honour retry_after when provided, else a small
        # backoff. sleep is injected so tests never actually wait.
        sleep(float(outcome.retry_after or 1))


# ---------------------------------------------------------------------------
# Error probe (AC1): cheap, safe verification of the error contract.
# ---------------------------------------------------------------------------


def probe_errors(
    module: str,
    request_style: str,
    account: str,
    probe_request: ProbeRequest,
    *,
    token: str | None,
    probe_auth: bool,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Verify the error contract with ONE invalid-field request (always) and,
    only when *probe_auth* is set, ONE invalid-token request (never destructive).

    Returns a deterministic dict:
        {"invalid_request": {"probed": bool, "classified": <class|None>,
                              "expected": "invalid_request", "match": bool},
         "invalid_token":   {"probed": bool, "classified": <class|None>, ...}}
    """
    result: dict[str, Any] = {}

    # Intentionally invalid FIELD -> must classify invalid_request.
    invalid_batch = [
        {
            "field_id": "__toorow_probe_invalid_field__",
            "source_field": "__toorow_probe_invalid_field__",
            "kind": "metric",
        }
    ]
    outcome = _request_with_retry(
        module,
        request_style,
        account,
        invalid_batch,
        probe_request,
        token=token,
        sleep=sleep,
        max_retries=3,
    )
    classified = None
    if outcome is not None:
        classified = outcome.rejected.get("__toorow_probe_invalid_field__")
        if classified is None and "__toorow_probe_invalid_field__" in outcome.unsupported:
            classified = "invalid_request"
    result["invalid_request"] = {
        "probed": True,
        "classified": classified,
        "expected": "invalid_request",
        "match": classified == "invalid_request",
    }

    if probe_auth:
        # Invalid TOKEN (opt-in). We pass a sentinel bad token; the request layer
        # never revokes anything. The classified class is whatever the error_map
        # maps the 401 to (auth_expired by default).
        auth_outcome = _request_with_retry(
            module,
            request_style,
            account,
            invalid_batch,
            probe_request,
            token="__toorow_probe_invalid_token__",
            sleep=sleep,
            max_retries=3,
        )
        auth_class = (
            auth_outcome.rejected.get("__toorow_probe_invalid_field__")
            if auth_outcome is not None
            else None
        )
        result["invalid_token"] = {
            "probed": True,
            "classified": auth_class,
            "expected": "auth_expired_or_revoked",
            "match": auth_class in ("auth_expired", "auth_revoked"),
        }
    else:
        result["invalid_token"] = {
            "probed": False,
            "classified": None,
            "expected": "auth_expired_or_revoked",
            "match": None,
        }
    return result


# ---------------------------------------------------------------------------
# Topology probe (AC1): discovery live, assert the selected account is reachable.
# ---------------------------------------------------------------------------


def probe_topology(
    module: str,
    account: str,
    discover: Callable[[], list[str]] | None,
) -> dict[str, Any]:
    """Run discovery (when declared) and assert *account* is in the reachable set.

    *discover* returns the flat list of reachable account ids (a test injects it;
    the real runner wires the module's discover_accounts). When the module does
    not declare an account_topology, *discover* is None and the probe is skipped.
    """
    if discover is None:
        return {"declared": False, "probed": False, "account_reachable": None}
    reachable = sorted(set(discover()))
    return {
        "declared": True,
        "probed": True,
        "account_reachable": account in reachable,
        "reachable_count": len(reachable),
    }


# ---------------------------------------------------------------------------
# Report assembly (AC1): deterministic, redacted, verdict-bearing.
# ---------------------------------------------------------------------------


def _redact_connection(connection_ref: str) -> str:
    """Redact a connection id to a short, non-reversible tail marker (AD-3)."""
    tail = connection_ref[-4:] if len(connection_ref) >= 4 else connection_ref
    return f"conn_***{tail}"


def _coverage(statuses: dict[str, str], total_targets: int) -> dict[str, int]:
    ok = sum(1 for s in statuses.values() if s == STATUS_OK)
    empty = sum(1 for s in statuses.values() if s == STATUS_EMPTY)
    unsupported = sum(1 for s in statuses.values() if s == STATUS_UNSUPPORTED)
    rejected = sum(
        1 for s in statuses.values() if s.startswith(STATUS_REJECTED_PREFIX + "(")
    )
    unprobed = total_targets - len(statuses)
    return {
        "ok": ok,
        "empty": empty,
        "rejected": rejected,
        "unsupported": unsupported,
        "unprobed": max(unprobed, 0),
    }


def _verdict(
    request_style: str,
    coverage: dict[str, int],
    error_probe: dict[str, Any],
    topology_probe: dict[str, Any],
) -> str:
    """Compute the report verdict (ratified | partial | failed).

    * structural ("none"): ratified iff every target is ok (by construction).
    * live: FAILED if any field is rejected/unsupported, or the invalid_request
      error contract does not hold, or a declared topology says the account is
      NOT reachable. RATIFIED if every probed field is ok/empty AND nothing is
      unprobed AND the error contract holds. Otherwise PARTIAL (progress made,
      coverage incomplete -- e.g. tier-core only, or some empties/unprobed).
    """
    if request_style == "none":
        return VERDICT_RATIFIED if coverage["unprobed"] == 0 else VERDICT_PARTIAL

    if coverage["rejected"] or coverage["unsupported"]:
        return VERDICT_FAILED
    if error_probe.get("invalid_request", {}).get("match") is False:
        return VERDICT_FAILED
    if topology_probe.get("declared") and topology_probe.get("account_reachable") is False:
        return VERDICT_FAILED

    clean = coverage["ok"] + coverage["empty"]
    total = clean + coverage["unprobed"]
    if coverage["unprobed"] == 0 and coverage["ok"] > 0 and clean == total:
        return VERDICT_RATIFIED
    return VERDICT_PARTIAL


def build_report(
    *,
    module: str,
    api_version: str,
    connection_ref: str,
    account: str,
    probed_at: str,
    tiers: tuple[str, ...],
    request_style: str,
    statuses: dict[str, str],
    total_targets: int,
    error_probe: dict[str, Any],
    topology_probe: dict[str, Any],
    structural_note: str | None,
) -> dict[str, Any]:
    """Assemble the deterministic, redacted ratification report (AC1)."""
    coverage = _coverage(statuses, total_targets)
    verdict = _verdict(request_style, coverage, error_probe, topology_probe)
    report: dict[str, Any] = {
        "schema_version": "1",
        "module": module,
        "api_version": api_version,
        "connection_ref": _redact_connection(connection_ref),
        "account": account,
        "probed_at": probed_at,
        "tiers": list(tiers),
        "request_style": request_style,
        "fields": dict(sorted(statuses.items())),
        "coverage": coverage,
        "error_probe": error_probe,
        "topology_probe": topology_probe,
        "verdict": verdict,
    }
    if structural_note:
        report["structural_note"] = structural_note
    return report


def serialize_report(report: dict[str, Any]) -> bytes:
    """Serialize canonical UTF-8 bytes (deterministic; no volatile metadata)."""
    return (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


def resolve_tiers(tier_arg: str) -> tuple[str, ...]:
    if tier_arg == "all":
        return VALID_TIERS
    if tier_arg not in VALID_TIERS:
        raise RatificationError(
            f"--tier must be one of core|standard|advanced|all (got {tier_arg!r})"
        )
    return (tier_arg,)


def run_ratification(
    *,
    module: str,
    connection_ref: str,
    account: str,
    tiers: tuple[str, ...],
    probed_at: str,
    probe_auth: bool,
    probe_request: ProbeRequest,
    discover: Callable[[], list[str]] | None,
    modules_dir: Path,
    token: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Full ratification pipeline for one module. Returns the report dict.

    Fully injectable (probe_request / discover / token / sleep) so the harness is
    testable with mocks and no network (AC4).
    """
    probe = load_probe_config(module, modules_dir)
    request_style = probe["request_style"]
    batch_size = int(probe.get("batch_size", 0))
    structural_note = probe.get("structural_note")
    api_version = load_api_version(module, modules_dir)
    targets = load_catalog_fields(module, modules_dir, tiers)
    total_targets = len(targets)

    if request_style == "none":
        # Structural ratification: catalog == contract by construction (AC2).
        statuses = {t["field_id"]: STATUS_OK for t in targets}
        error_probe = {
            "invalid_request": {
                "probed": False,
                "classified": None,
                "expected": "invalid_request",
                "match": None,
            },
            "invalid_token": {
                "probed": False,
                "classified": None,
                "expected": "auth_expired_or_revoked",
                "match": None,
            },
        }
        topology_probe = {"declared": False, "probed": False, "account_reachable": None}
    else:
        statuses = probe_fields(
            module,
            request_style,
            batch_size,
            account,
            targets,
            probe_request,
            token=token,
            sleep=sleep,
        )
        error_probe = probe_errors(
            module,
            request_style,
            account,
            probe_request,
            token=token,
            probe_auth=probe_auth,
            sleep=sleep,
        )
        topology_probe = probe_topology(module, account, discover)

    return build_report(
        module=module,
        api_version=api_version,
        connection_ref=connection_ref,
        account=account,
        probed_at=probed_at,
        tiers=tiers,
        request_style=request_style,
        statuses=statuses,
        total_targets=total_targets,
        error_probe=error_probe,
        topology_probe=topology_probe,
        structural_note=structural_note if request_style == "none" else None,
    )


def default_out_path(module: str, probed_at: str, modules_dir: Path) -> Path:
    """Deterministic report path: reports/ratification-<probed_at>.json.

    ``:`` is stripped from the timestamp so the filename is filesystem-safe and
    the glob ``ratification-*.json`` (fail-closed export) matches it.
    """
    safe = probed_at.replace(":", "").replace("+", "Z")
    return _module_dir(module, modules_dir) / "reports" / f"ratification-{safe}.json"


# ---------------------------------------------------------------------------
# Real (live) probe_request + discover wiring -- used only when run as a CLI.
# These reach the network; the unit tests never touch them (they inject mocks).
# ---------------------------------------------------------------------------


def _live_probe_request_factory(connection_ref: str) -> ProbeRequest:  # pragma: no cover
    """Build the real HTTP probe_request bound to a Nango/Google token path.

    NOTE: the concrete per-style HTTP request builders live here so core stays
    untouched (scripts-only). This path is exercised only during a real,
    human-gated live run against an actual account -- it is intentionally NOT
    covered by the mocked unit tests (AC4).
    """
    raise RatificationError(
        "Live probe execution is human-gated and not wired for an unattended run. "
        "Provide a --dry-run structural target, or run the documented live "
        "procedure (see server/modules/README.md section 7)."
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True, help="Module name (e.g. meta-ads).")
    parser.add_argument(
        "--connection", required=True, help="Connection ref id (redacted in the report)."
    )
    parser.add_argument(
        "--account",
        default="",
        help="Selected account id (topology-resolved). Required for live tiers.",
    )
    parser.add_argument("--tier", default="core", help="core|standard|advanced|all")
    parser.add_argument(
        "--probe-auth",
        action="store_true",
        help="Opt-in: also issue ONE invalid-token request (never revokes).",
    )
    parser.add_argument(
        "--probed-at",
        default=None,
        help="ISO-8601 timestamp for the report (default: real clock at run time).",
    )
    parser.add_argument("--out", type=Path, default=None, help="Report output path.")
    parser.add_argument("--modules-dir", type=Path, default=_SERVER_DIR / "modules")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- CLI wrapper
    args = _parse_args(argv)
    try:
        from datetime import datetime, timezone  # noqa: PLC0415

        tiers = resolve_tiers(args.tier)
        probed_at = args.probed_at or (
            datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
        )
        probe = load_probe_config(args.module, args.modules_dir)
        if probe["request_style"] != "none" and not args.account:
            raise RatificationError(
                f"{args.module}: --account is required for a live "
                f"{probe['request_style']} probe (topology-resolved account id)."
            )
        probe_request = _live_probe_request_factory(args.connection)
        report = run_ratification(
            module=args.module,
            connection_ref=args.connection,
            account=args.account,
            tiers=tiers,
            probed_at=probed_at,
            probe_auth=args.probe_auth,
            probe_request=probe_request,
            discover=None,
            modules_dir=args.modules_dir,
        )
        out = args.out or default_out_path(args.module, probed_at, args.modules_dir)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(serialize_report(report))
        print(f"Ratification report written to: {out}")
        print(f"verdict={report['verdict']} coverage={report['coverage']}")
        return 0
    except RatificationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
