"""Render a migrated screen in the sandbox and capture it.

    pnpm --filter @toorow/admin dev
    python scripts/shoot_screen.py ModulesCatalog RegressionRuns

The screens fetch from the API, so the API is answered here with a fixture.
The fixtures are shaped like the real payloads and carry NO production
identifiers — `proj_EXAMPLE`, `example.com` — per the repository rule that
nothing traceable to a customer appears in the tree.

Each screen is captured twice: with data, and with the API refusing, because a
screen's error surface is part of what has to be looked at and is the state
nobody ever checks.
"""

import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5199"
OUT = pathlib.Path("scratch/screens")

# Shaped like the real responses. Keys are matched as substrings of the URL.
FIXTURES: dict[str, object] = {
    "/api/modules/available": [
        {"module_name": "google_ads", "display_name": "Google Ads", "enabled": True,
         "explicitly_set": True, "active_connections": 3},
        {"module_name": "google-analytics", "display_name": "Google Analytics 4", "enabled": True,
         "explicitly_set": False, "active_connections": 2},
        {"module_name": "search_console", "display_name": "Search Console", "enabled": True,
         "explicitly_set": False, "active_connections": 1},
        {"module_name": "meta_ads", "display_name": "Meta Ads", "enabled": False,
         "explicitly_set": True, "active_connections": 0},
        {"module_name": "linkedin_ads", "display_name": "LinkedIn Ads", "enabled": False,
         "explicitly_set": False, "active_connections": 0},
    ],
    # Field names are the wire contract, not the component's: `score_passed`,
    # `score_total`, `precision_pct`. The first fixture guessed `scored`/
    # `total`/`precision` and the screen rendered its schema-error state —
    # correctly. A screen that refuses a payload it does not recognise instead
    # of guessing is the behaviour this project keeps asking for, so the
    # fixture was wrong and the screen was right.
    # Every one of these screens checks that the response echoes the project and
    # datastream it asked for, and refuses it otherwise. Three fixtures in a row
    # were rejected for missing that echo before I stopped treating it as a bug.
    # It is a consistent discipline in this codebase and worth not undoing.
    "/sample": {
        "project_id": "proj_EXAMPLE", "datastream_id": "ds_EXAMPLE",
        "stage": "published", "served_stage": "published",
        "stage_note": "Curated is the governed default for this Datastream.",
        "datastream": {"id": "ds_EXAMPLE", "name": "GA4 — web", "module_name": "google-analytics",
                       "source_kind": "connector"},
        "collection_expected": True, "materialization_available": True,
        "version_binding_available": True, "sample_watermark": "2026-07-28",
        "masked_value_count": 128, "masked_fields": ["user_id", "email"],
        "days": [
            {"date": "2026-07-28", "sampled_row_count": 3, "rejection_count": 0, "field_count": 6,
             "rows": [
                 {"date": "2026-07-28", "campaign": "Summer sale", "source": "google", "sessions": 1284, "revenue": 4210.5, "user_id": "***"},
                 {"date": "2026-07-28", "campaign": "Brand", "source": "direct", "sessions": 902, "revenue": 1180.0, "user_id": "***"},
                 {"date": "2026-07-28", "campaign": "Retargeting", "source": "meta", "sessions": 431, "revenue": 890.25, "user_id": "***"},
             ]},
            {"date": "2026-07-27", "sampled_row_count": 2, "rejection_count": 2, "field_count": 6,
             "rows": [
                 {"date": "2026-07-27", "campaign": "Summer sale", "source": "google", "sessions": 1190, "revenue": 3980.0, "user_id": "***"},
                 {"date": "2026-07-27", "campaign": "Brand", "source": "direct", "sessions": 845, "revenue": 1020.75, "user_id": "***"},
             ]},
        ],
    },
    # The ledger drives CoverageBars: one entry per calendar day, each with the
    # ledger's own status word. `empty` and `never_fetched` are BOTH present on
    # purpose — they look alike and mean opposite things, and the strip has to
    # show that it tells them apart.
    "/ledger": {
        "ledger": [
            {"date": f"2026-06-{day:02d}", "status": status, "row_count": rows}
            for day, status, rows in [
                (i, "ok", 18000 + i * 37) for i in range(1, 19)
            ]
        ] + [
            {"date": "2026-06-19", "status": "partial", "row_count": 4200},
            {"date": "2026-06-20", "status": "empty", "row_count": 0},
            {"date": "2026-06-21", "status": "empty", "row_count": 0},
            {"date": "2026-06-22", "status": "failed", "row_count": None},
            {"date": "2026-06-23", "status": "never_fetched", "row_count": None},
            {"date": "2026-06-24", "status": "never_fetched", "row_count": None},
            {"date": "2026-06-25", "status": "running", "row_count": None},
        ] + [
            {"date": f"2026-06-{day:02d}", "status": "ok", "row_count": 18500 + day}
            for day in range(26, 31)
        ],
    },
    "/read-model": {
        "project_id": "proj_EXAMPLE",
        "datastream_id": "ds_EXAMPLE",
        "datastream": {"id": "ds_EXAMPLE", "name": "GA4 — web", "module_name": "google-analytics",
                       "source_kind": "connector", "next_run_at": "2026-07-30T02:00:00Z"},
        # The overview tab cross-checks these against each other: the pointer
        # must name the published execution, and an execution that IS the latest
        # must actually be in the published state. Both are asserted here.
        "current_published_execution_id": "exec_EXAMPLE",
        "published_execution": {
            "id": "exec_EXAMPLE", "state": "published",
            "plan_version_id": "pv_0003", "mapping_version_id": "mv_0003",
            "state_changed_at": "2026-07-28T03:12:00Z", "row_count": 18420,
            "dq_state": "complete",
        },
        "latest_execution": {"id": "exec_EXAMPLE", "state": "published"},
        "current_candidate": None,
        "plan_versions": [{"id": "pv_0003", "version_number": 12}],
        "publication_log": [],
        "recent_imports": [
            {"id": f"imp_{i}", "snapshot_observed_at": f"2026-07-{28 - i:02d}T03:05:00Z",
             "outcome": outcome, "row_count": rows, "rejected_row_count": rejected}
            for i, (outcome, rows, rejected) in enumerate([
                ("published", 18420, 0), ("published", 18102, 12), ("noop", 0, 0),
                ("published", 17988, 4), ("rejected", 0, 1204), ("published", 18301, 0),
            ])
        ],
        "mapping_versions": [
            {
                "id": "mv_0003", "datastream_id": "ds_EXAMPLE", "version_number": 3,
                "plan_version_id": "pv_0003", "executable": False, "blocking_count": 1,
                "ossie_spec_version": "0.4.0", "created_at": "2026-07-28T09:10:00Z",
                "ossie_projection": {"metrics": [{"name": "sessions", "expression": "SUM(sessions)"}]},
                "mapping_payload": {
                    "mapping_contract_version": "1.2.0", "source_schema_hash": "sha256:EXAMPLE",
                    "plan_version_id": "pv_0003", "grain": ["date", "session_source"],
                    "ambiguities": [{"code": "AMBIGUOUS_CURRENCY", "path": "$.revenue",
                                     "field_ids": ["revenue"], "candidates": ["EUR", "USD"], "repair": {}}],
                    "fields": [
                        {"field_id": "date", "physical_type": "DATE",
                         "profile": {"nullable": False, "unique": False, "cardinality_signal": "high",
                                     "sample_values": ["2026-07-01"], "confidence": 0.99},
                         "suggestion": {"semantic_role": "dimension", "aggregation": "none",
                                        "non_additive": True, "currency": "n/a", "sensitivity": "public",
                                        "status": "confirmed", "evidence": ["Matched on name and type"]},
                         "binding": {"canonical_target": "dim_date.date", "mdm_target": None,
                                     "status": "confirmed", "blocking_reason": None,
                                     "confirmed_by": "owner@example.com", "confirmed_reason": "Reviewed"}},
                        {"field_id": "session_source", "physical_type": "STRING",
                         "profile": {"nullable": True, "unique": False, "cardinality_signal": "medium",
                                     "sample_values": ["google"], "confidence": 0.87},
                         "suggestion": {"semantic_role": "dimension", "aggregation": "none",
                                        "non_additive": True, "currency": "n/a", "sensitivity": "public",
                                        "status": "suggested", "evidence": ["Name similarity 0.87"]},
                         "binding": {"canonical_target": "dim_channel.source", "mdm_target": None,
                                     "status": "suggested", "blocking_reason": None,
                                     "confirmed_by": None, "confirmed_reason": None}},
                        {"field_id": "sessions", "physical_type": "INT64",
                         "profile": {"nullable": False, "unique": False, "cardinality_signal": "low",
                                     "sample_values": ["1284"], "confidence": 0.95},
                         "suggestion": {"semantic_role": "metric", "aggregation": "sum",
                                        "non_additive": False, "currency": "n/a", "sensitivity": "public",
                                        "status": "resolved", "evidence": ["Additive integer"]},
                         "binding": {"canonical_target": "fct_traffic.sessions", "mdm_target": None,
                                     "status": "resolved", "blocking_reason": None,
                                     "confirmed_by": None, "confirmed_reason": None}},
                        {"field_id": "revenue", "physical_type": "NUMERIC",
                         "profile": {"nullable": True, "unique": False, "cardinality_signal": "low",
                                     "sample_values": ["1240.50"], "confidence": 0.62},
                         "suggestion": {"semantic_role": "metric", "aggregation": "sum",
                                        "non_additive": False, "currency": "unknown",
                                        "sensitivity": "restricted", "status": "blocking",
                                        "evidence": ["Currency not declared by the source"]},
                         "binding": {"canonical_target": None, "mdm_target": None, "status": "blocking",
                                     "blocking_reason": "The source declares no currency for this money column.",
                                     "confirmed_by": None, "confirmed_reason": None}},
                    ],
                },
            },
            {
                "id": "mv_0002", "datastream_id": "ds_EXAMPLE", "version_number": 2,
                "plan_version_id": "pv_0002", "executable": True, "blocking_count": 0,
                "ossie_spec_version": "0.4.0", "created_at": "2026-07-21T09:10:00Z",
                "ossie_projection": None,
                "mapping_payload": {"mapping_contract_version": "1.2.0",
                                    "source_schema_hash": "sha256:EXAMPLE", "plan_version_id": "pv_0002",
                                    "grain": ["date"], "ambiguities": [], "fields": []},
            },
        ],
    },
    # `RegressionRuns` reads TWO endpoints. Only the first was fixtured, so the
    # screen always rendered its honest refusal -- and, until the payload check
    # landed, printed a raw TypeError inside it. A half-fixtured screen is a
    # screen the harness cannot judge.
    "/run-profiles": {
        "run_profiles": [
            {"id": "rp_EXAMPLE_1", "name": "Acquisition — daily", "evidence_mode": "offline",
             "question_set_version": 4, "as_of": "2026-07-28", "member_count": 48},
            {"id": "rp_EXAMPLE_2", "name": "Observed — last 7 days", "evidence_mode": "observed_cohort",
             "question_set_version": 4, "as_of": "2026-08-02", "member_count": 120},
        ],
    },
    "/api/eval/runs": {
        "runs": [
            {"id": "run_0007", "run_at": "2026-07-28T09:10:00Z", "score_passed": 48,
             "score_total": 48, "precision_pct": 94.0, "regressions": 0, "status": "passed"},
            {"id": "run_0006", "run_at": "2026-07-27T09:10:00Z", "score_passed": 45,
             "score_total": 48, "precision_pct": 88.5, "regressions": 3, "status": "regressed"},
            {"id": "run_0005", "run_at": "2026-07-26T09:10:00Z", "score_passed": 48,
             "score_total": 48, "precision_pct": 93.2, "regressions": 0, "status": "passed"},
            {"id": "run_0004", "run_at": "2026-07-25T09:10:00Z", "score_passed": 46,
             "score_total": 48, "precision_pct": 90.1, "regressions": 2, "status": "regressed"},
        ]
    },
}


def body_for(url: str):
    for key, value in FIXTURES.items():
        if key in url:
            if key == "/sample":
                # This screen refuses a response that does not ECHO the stage,
                # interval and limit it asked for, so the fixture reads them
                # back off the query string rather than hard-coding a guess.
                # Three fixtures were rejected before I stopped assuming the
                # screen was wrong: it is checking that the answer belongs to
                # the question, which is exactly right.
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(url).query)
                one = lambda k, d: q.get(k, [d])[0]
                echoed = dict(value)
                echoed["stage"] = one("stage", "published")
                echoed["date_from"] = one("date_from", "2026-07-23")
                echoed["date_to"] = one("date_to", "2026-07-29")
                echoed["limit"] = int(one("limit", "5"))
                days = []
                for i, day in enumerate(value["days"]):
                    d = dict(day)
                    d["date"] = echoed["date_to"] if i == 0 else echoed["date_from"]
                    days.append(d)
                echoed["days"] = days  # counts must equal len(rows): a bounded contract
                echoed["sample_watermark"] = echoed["date_to"]
                return echoed
            return value
    return {}


def main() -> int:
    names = sys.argv[1:]
    if not names:
        print("usage: shoot_screen.py <ScreenName> [ScreenName ...]")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name in names:
            for mode in ("data", "error"):
                page = browser.new_page(
                    viewport={"width": 1440, "height": 1000}, device_scale_factor=2
                )
                problems: list[str] = []
                page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
                page.on(
                    "console",
                    lambda m: problems.append(f"console.error: {m.text}")
                    if m.type == "error"
                    else None,
                )

                if mode == "data":
                    page.route(
                        "**/api/**",
                        lambda route: route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(body_for(route.request.url)),
                        ),
                    )
                else:
                    page.route(
                        "**/api/**",
                        lambda route: route.fulfill(
                            status=503,
                            content_type="application/json",
                            body=json.dumps({"detail": "upstream unavailable"}),
                        ),
                    )

                # `page.route` alone cannot produce the refusing state for every
                # screen: the Data and Datastream sandboxes replace `window.fetch`
                # in their own component body, so no request ever reaches the
                # network layer for Playwright to intercept. Measured 2026-08-03,
                # `DataWorkspace-data.png` and `DataWorkspace-error.png` were
                # byte-identical — six screens whose error surface the harness
                # reported as captured and had never once shown.
                #
                # `api=refusing` asks those sandboxes to refuse from the inside.
                # It is harmless for the screens that do issue real requests:
                # they keep being refused by the route above.
                suffix = "&api=refusing" if mode == "error" else ""
                page.goto(f"{BASE}/debug/screen?name={name}{suffix}", wait_until="networkidle")
                page.wait_for_timeout(1200)
                path = OUT / f"{name}-{mode}.png"
                page.screenshot(path=str(path), full_page=True)
                print(f"  {path}")
                for problem in problems[:4]:
                    print(f"    ! {problem}")
                page.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
