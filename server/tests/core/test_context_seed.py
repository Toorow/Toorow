"""toorow -- Unit tests for context_seed.py (Story 44.2).

The fake connection is a small in-memory Postgres stand-in: it dispatches on the
normalised SQL text and keeps real rows, so the idempotency / skip-rule
assertions inspect actual stored state and actual bound parameters (never
`x in some_tuple` membership, which the wave-1 review showed can mask a bug).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from core import context_seed
from core.context_seed import (
    SEED_ACTOR,
    SEED_BANNER,
    UniqueViolation,
    build_connector_topic_body,
    connector_topic_title,
    load_registry_entry,
    seed_project_context,
    seed_project_context_best_effort,
)

PROJECT_ID = "proj_seed_test"
MODULE_NAME = "demo-source"


@pytest.fixture()
def anyio_backend():
    """Pin anyio to asyncio (trio is not a project dep) -- suite convention."""
    return "asyncio"


# ---------------------------------------------------------------------------
# Registry fixture on disk
# ---------------------------------------------------------------------------


MANIFEST: dict[str, Any] = {
    "schema_version": "1.2",
    "name": MODULE_NAME,
    "display_name": "Demo Source",
    "auth_type": "oauth2",
    "module_kind": "kpi",
    "public_catalog": {
        "visibility": "public",
        "category": "paid_media",
        "onboarding_modes": ["connector_pull"],
        "verification": {
            "status": "blocked",
            "reason_code": "live_evidence_not_ratified",
            "follow_up": "Ratify and run the independent connector evidence contract.",
        },
    },
    "report_profiles": [
        {"id": "campaign_daily", "display_name": "Campaign Performance (daily)"},
        {"id": "creative_daily", "display_name": "Creative Performance (daily)"},
    ],
    "canonical_metric_mapping": {"spend": "cost"},
    "canonical_dimension_mapping": {"campaign_id": "campaign_id"},
    "source_capabilities": {
        "contract_version": "1",
        "fields": [
            {
                "field_id": "spend",
                "kind": "metric",
                "description": "spend exposed by Demo Source.",
            }
        ],
        "reports": [
            {
                "id": "campaign_daily",
                "availability": {"status": "selectable"},
                "metrics": ["spend"],
                "dimensions": ["date", "campaign_id"],
            },
            {
                "id": "creative_daily",
                "availability": {
                    "status": "unavailable",
                    "reason_code": "provider_endpoint_missing",
                    "follow_up": "Revisit when the provider ships the creative report.",
                },
                "metrics": [],
                "dimensions": [],
            },
        ],
    },
}

CATALOG: dict[str, Any] = {
    "connector": MODULE_NAME,
    "catalog_version": "1",
    "fields": [
        {"field_id": "spend", "kind": "metric", "tier": "core", "exposure": "exposed"},
        {
            "field_id": "frequency",
            "kind": "metric",
            "tier": "advanced",
            "exposure": "planned",
        },
    ],
    "sources": [
        {"kind": "official", "url": "https://example.invalid/docs", "fetched_at": "2026-07-21"}
    ],
}

RELATIONS = ["stg_demo_source_daily", "stg_demo_source_creative"]


@pytest.fixture()
def registry_dir(tmp_path: Path) -> Path:
    """Write a minimal on-disk connector registry for MODULE_NAME."""
    module_dir = tmp_path / MODULE_NAME
    (module_dir / "dbt" / "staging").mkdir(parents=True)
    (module_dir / "manifest.json").write_text(
        json.dumps(MANIFEST), encoding="utf-8"
    )
    (module_dir / "api_catalog.json").write_text(json.dumps(CATALOG), encoding="utf-8")
    for relation in RELATIONS:
        (module_dir / "dbt" / "staging" / f"{relation}.sql").write_text(
            "select 1", encoding="utf-8"
        )
    (module_dir / "dbt" / "staging" / "schema.yml").write_text("version: 2", encoding="utf-8")
    return tmp_path


def _add_module(registry_dir: Path, name: str, display_name: str) -> None:
    """Add a second, minimal connector module to an existing registry fixture."""
    module_dir = registry_dir / name
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "manifest.json").write_text(
        json.dumps({"name": name, "display_name": display_name}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# In-memory Postgres stand-in
# ---------------------------------------------------------------------------


TOPIC_COLS = [
    "id",
    "project_id",
    "title",
    "body_md",
    "status",
    "owner",  # 44.11 -- nullable explicit owner, in create_topic's RETURNING
    "created_by",
    "created_at",
    "updated_at",
]

EDGE_COLS = [
    "id",
    "from_id",
    "from_type",
    "to_id",
    "to_type",
    "edge_type",
    "project_id",
    "created_by",
    "created_at",
]


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class FakeDB:
    """Row store shared by every cursor of a FakeConn."""

    def __init__(self) -> None:
        self.modules: list[str] = []
        # (schema_context_id, relation)
        self.schema_docs: list[tuple[str, str]] = []
        self.topics: dict[str, dict[str, Any]] = {}
        self.topic_versions: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self.audit: list[tuple[Any, ...]] = []
        self.calls: list[tuple[str, Any]] = []
        self.savepoints: list[str] = []
        #: Simulated concurrency. When set, the FIRST topic INSERT carrying this
        #: title behaves like a lost race: a rival row appears (as another
        #: transaction's committed write would) and the INSERT raises.
        self.topic_race_title: str | None = None
        self.topic_race_status: str = "active"
        #: Same idea for the edge insert: raise UniqueViolation N times.
        self.edge_race_remaining: int = 0
        self._seq = 0

    def next_ts(self) -> str:
        self._seq += 1
        return f"2026-07-26T00:00:{self._seq:02d}Z"

    def latest_version(self, topic_id: str) -> dict[str, Any] | None:
        rows = [v for v in self.topic_versions if v["topic_id"] == topic_id]
        return max(rows, key=lambda v: v["version_number"]) if rows else None


class FakeCursor:
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self.description: list[tuple[str]] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    # -- helpers ---------------------------------------------------------
    def _set(self, cols: list[str], rows: list[tuple[Any, ...]]) -> None:
        self.description = [(c,) for c in cols]
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    # -- the dispatcher --------------------------------------------------
    def execute(self, sql: str, params: Any = None) -> None:  # noqa: C901
        sql = _norm(sql)
        self.db.calls.append((sql, params))
        self._rows = []

        if sql.startswith(("SAVEPOINT ", "RELEASE SAVEPOINT ", "ROLLBACK TO SAVEPOINT ")):
            self.db.savepoints.append(sql)
            return

        if sql.startswith("INSERT INTO app.audit_log"):
            self.db.audit.append(tuple(params))
            return

        if sql.startswith("INSERT INTO app.context_topics_versions"):
            # 44.11 added `owner` at index 5: create path = 10 params
            # (version_number literal 1), update path = 11 (version_number bound).
            if len(params) == 10:
                version_number, changed_by = 1, params[9]
            else:
                version_number, changed_by = params[9], params[10]
            self.db.topic_versions.append(
                {
                    "topic_id": params[0],
                    "project_id": params[1],
                    "title": params[2],
                    "body_md": params[3],
                    "status": params[4],
                    "owner": params[5],
                    "created_by": params[6],
                    "version_number": version_number,
                    "changed_by": changed_by,
                }
            )
            return

        if sql.startswith("INSERT INTO app.context_topics"):
            # 44.11: create_topic binds (id, project_id, title, body_md, owner,
            # created_by) -- owner sits between body_md and created_by.
            topic_id, project_id, title, body_md, owner, created_by = params
            if title == self.db.topic_race_title:
                # A rival transaction committed first: its row is now visible
                # and the unique index (migration 113) rejects ours.
                self.db.topic_race_title = None
                rival_id = f"top_rival_{title}"
                now = self.db.next_ts()
                self.db.topics[rival_id] = {
                    "id": rival_id,
                    "project_id": None,
                    "title": title,
                    "body_md": "written by the racing seed",
                    "status": self.db.topic_race_status,
                    "owner": None,
                    "created_by": SEED_ACTOR,
                    "created_at": now,
                    "updated_at": now,
                }
                raise UniqueViolation(
                    'duplicate key value violates unique constraint '
                    '"uq_context_topics_title_platform"'
                )
            now = self.db.next_ts()
            row = {
                "id": topic_id,
                "project_id": project_id,
                "title": title,
                "body_md": body_md,
                "status": "active",
                "owner": owner,
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
            }
            self.db.topics[topic_id] = row
            self._set(TOPIC_COLS, [tuple(row[c] for c in TOPIC_COLS)])
            return

        if sql.startswith("INSERT INTO app.context_graph"):
            if self.db.edge_race_remaining > 0:
                self.db.edge_race_remaining -= 1
                raise UniqueViolation(
                    "duplicate key value violates unique constraint on app.context_graph"
                )
            row = dict(zip(EDGE_COLS[:-1], params))
            row["created_at"] = self.db.next_ts()
            self.db.edges.append(row)
            self._set(EDGE_COLS, [tuple(row[c] for c in EDGE_COLS)])
            return

        if sql.startswith("UPDATE app.context_topics SET title"):
            # 44.11: update_topic binds (title, body_md, status, owner, id).
            new_title, new_body, new_status, new_owner, topic_id = params
            row = self.db.topics[topic_id]
            row.update(
                title=new_title,
                body_md=new_body,
                status=new_status,
                owner=new_owner,
                updated_at=self.db.next_ts(),
            )
            self._set(TOPIC_COLS, [tuple(row[c] for c in TOPIC_COLS)])
            return

        if sql.startswith("SELECT DISTINCT module_name FROM app.datastreams"):
            assert params == (PROJECT_ID,)
            self._set(["module_name"], [(m,) for m in self.db.modules])
            return

        if sql.startswith("SELECT id, relation FROM app.schema_context"):
            assert params == (PROJECT_ID,)
            self._set(["id", "relation"], list(self.db.schema_docs))
            return

        if sql.startswith("SELECT id, body_md, status FROM app.context_topics"):
            title = params[0]
            matches = sorted(
                (
                    r
                    for r in self.db.topics.values()
                    if r["project_id"] is None and r["title"] == title
                ),
                # ORDER BY (status = 'archived'), created_at
                key=lambda r: (r["status"] == "archived", r["created_at"]),
            )
            self._set(
                ["id", "body_md", "status"],
                [(m["id"], m["body_md"], m["status"]) for m in matches[:1]],
            )
            return

        if sql.startswith("SELECT changed_by FROM app.context_topics_versions"):
            latest = self.db.latest_version(params[0])
            self._set(["changed_by"], [(latest["changed_by"],)] if latest else [])
            return

        if sql.startswith("SELECT COALESCE(MAX(version_number), 0)"):
            rows = [
                v["version_number"]
                for v in self.db.topic_versions
                if v["topic_id"] == params[0]
            ]
            self._set(["max"], [(max(rows) if rows else 0,)])
            return

        if sql.startswith("SELECT 1 FROM app.context_graph"):
            from_id, to_id, edge_type = params
            hit = any(
                e["from_id"] == from_id
                and e["to_id"] == to_id
                and e["edge_type"] == edge_type
                for e in self.db.edges
            )
            self._set(["?column?"], [(1,)] if hit else [])
            return

        if sql.startswith("SELECT 1 FROM app.context_topics"):
            node_id = params[0]
            row = self.db.topics.get(node_id)
            self._set(["?column?"], [(1,)] if row and row["status"] == "active" else [])
            return

        if sql.startswith("SELECT 1 FROM app.schema_context"):
            hit = any(doc_id == params[0] for doc_id, _ in self.db.schema_docs)
            self._set(["?column?"], [(1,)] if hit else [])
            return

        if sql.startswith(
            "SELECT id, project_id, title, body_md, status, owner, created_by, "
            "created_at, updated_at FROM app.context_topics"
        ):
            # update_topic's FOR UPDATE read -- column list includes owner (44.11).
            row = self.db.topics[params[0]]
            self._set(TOPIC_COLS, [tuple(row[c] for c in TOPIC_COLS)])
            return

        raise AssertionError(f"unexpected SQL in fake connection: {sql}")


class FakeConn:
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.db)

    def commit(self) -> None:
        self.commits += 1


def _seeded_db() -> FakeDB:
    db = FakeDB()
    db.modules = [MODULE_NAME]
    db.schema_docs = [
        ("sctx_1", "stg_demo_source_daily"),
        ("sctx_2", "stg_demo_source_daily"),
        ("sctx_3", "stg_demo_source_creative"),
        ("sctx_4", "stg_other_connector_daily"),
    ]
    return db


def _run(db: FakeDB, registry_dir: Path, **kwargs: Any) -> dict[str, Any]:
    return seed_project_context(
        FakeConn(db),
        PROJECT_ID,
        modules_dir=registry_dir,
        ensure_schema=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Registry reads / body composition
# ---------------------------------------------------------------------------


def test_load_registry_entry_reads_manifest_catalog_and_relations(registry_dir: Path):
    entry = load_registry_entry(MODULE_NAME, modules_dir=registry_dir)

    assert entry is not None
    assert entry["manifest"]["display_name"] == "Demo Source"
    assert entry["catalog"]["connector"] == MODULE_NAME
    # schema.yml is not a relation; the two staging models are, sorted.
    assert entry["relations"] == sorted(RELATIONS)


def test_load_registry_entry_missing_module_returns_none(registry_dir: Path):
    assert load_registry_entry("no-such-module", modules_dir=registry_dir) is None


def test_topic_body_is_verbatim_registry_text():
    body = build_connector_topic_body(MANIFEST, CATALOG)

    assert body.splitlines()[0] == SEED_BANNER
    # Verbatim manifest strings, not paraphrases.
    assert "spend exposed by Demo Source." in body
    assert "Campaign Performance (daily)" in body
    assert "live_evidence_not_ratified" in body
    assert "Ratify and run the independent connector evidence contract." in body
    assert "Revisit when the provider ships the creative report." in body
    assert "https://example.invalid/docs" in body
    # Deterministic: same inputs, same bytes (no LLM, no timestamps).
    assert build_connector_topic_body(MANIFEST, CATALOG) == body


def test_connector_topic_title_is_the_idempotency_key():
    assert connector_topic_title(MANIFEST) == "Connector: Demo Source"


# ---------------------------------------------------------------------------
# Seeding: topics + edges
# ---------------------------------------------------------------------------


def test_seed_creates_platform_topic_and_describes_edges(registry_dir: Path):
    db = _seeded_db()

    summary = _run(db, registry_dir)

    assert summary["topics_created"] == 1
    assert summary["modules"] == [MODULE_NAME]

    (topic,) = list(db.topics.values())
    assert topic["title"] == "Connector: Demo Source"
    assert topic["project_id"] is None, "seeded connector topics are platform-scoped"
    assert topic["created_by"] == SEED_ACTOR
    assert topic["body_md"].startswith(SEED_BANNER)

    # Version 1 is appended by create_topic with the system actor.
    version = db.latest_version(topic["id"])
    assert version["version_number"] == 1
    assert version["changed_by"] == SEED_ACTOR

    # Three docs belong to this module's relations; the fourth belongs to
    # another connector and must NOT be linked.
    assert summary["edges_created"] == 3
    linked = sorted(edge["to_id"] for edge in db.edges)
    assert linked == ["sctx_1", "sctx_2", "sctx_3"]
    for edge in db.edges:
        assert edge["from_id"] == topic["id"]
        assert edge["from_type"] == "topic"
        assert edge["to_type"] == "schema_doc"
        assert edge["edge_type"] == "describes"
        assert edge["project_id"] == PROJECT_ID
        assert edge["created_by"] == SEED_ACTOR


def test_seed_twice_creates_no_duplicates(registry_dir: Path):
    db = _seeded_db()

    first = _run(db, registry_dir)
    topics_after_first = len(db.topics)
    edges_after_first = len(db.edges)
    versions_after_first = len(db.topic_versions)

    second = _run(db, registry_dir)

    assert first["topics_created"] == 1
    assert second["topics_created"] == 0
    assert second["topics_updated"] == 0
    assert second["edges_created"] == 0
    assert len(db.topics) == topics_after_first == 1
    assert len(db.edges) == edges_after_first == 3
    assert len(db.topic_versions) == versions_after_first == 1


def test_seed_without_matching_schema_docs_creates_topic_only(registry_dir: Path):
    db = FakeDB()
    db.modules = [MODULE_NAME]
    db.schema_docs = [("sctx_9", "stg_unrelated_daily")]

    summary = _run(db, registry_dir)

    assert summary["topics_created"] == 1
    assert summary["edges_created"] == 0
    assert db.edges == []


def test_seed_restricted_to_landed_module_ignores_other_modules(registry_dir: Path):
    db = _seeded_db()
    db.modules = [MODULE_NAME, "another-module"]

    summary = _run(db, registry_dir, module_names=[MODULE_NAME])

    assert summary["modules"] == [MODULE_NAME]
    assert len(db.topics) == 1
    # The datastreams table is never queried when the module list is supplied.
    assert not any(
        sql.startswith("SELECT DISTINCT module_name") for sql, _ in db.calls
    )


def test_seed_skips_unknown_module_without_raising(registry_dir: Path):
    db = FakeDB()
    db.modules = ["ghost-module"]

    summary = _run(db, registry_dir)

    assert summary["topics_created"] == 0
    assert summary["modules"] == []
    assert db.topics == {}


# ---------------------------------------------------------------------------
# Skip rule: never overwrite a human edit
# ---------------------------------------------------------------------------


def test_seed_never_overwrites_a_human_edited_topic(registry_dir: Path):
    db = _seeded_db()
    _run(db, registry_dir)
    (topic_id,) = list(db.topics)

    # A curator rewrites the topic through the normal API (version 2).
    db.topics[topic_id]["body_md"] = "Human curated body."
    db.topic_versions.append(
        {
            "topic_id": topic_id,
            "project_id": None,
            "title": db.topics[topic_id]["title"],
            "body_md": "Human curated body.",
            "status": "active",
            "created_by": SEED_ACTOR,
            "version_number": 2,
            "changed_by": "curator@example.com",
        }
    )

    summary = _run(db, registry_dir)

    assert summary["topics_skipped_human"] == 1
    assert summary["topics_updated"] == 0
    assert summary["modules"] == []
    assert summary["modules_skipped"] == [MODULE_NAME]
    assert db.topics[topic_id]["body_md"] == "Human curated body."
    assert len(db.topic_versions) == 2
    assert db.latest_version(topic_id)["changed_by"] == "curator@example.com"


def test_seed_never_overwrites_a_topic_with_no_version_rows(registry_dir: Path):
    """Fail CLOSED: unknown authorship (no version row) is human-owned.

    A topic can exist without any version row -- inserted by a migration, by a
    direct fix-up, or by an older writer. The previous rule read
    ``author is not None and author != SEED_ACTOR``, so ``None`` fell through to
    the update branch and the seed silently rewrote a body it never authored.
    """
    db = _seeded_db()
    now = db.next_ts()
    db.topics["top_no_history"] = {
        "id": "top_no_history",
        "project_id": None,
        "title": "Connector: Demo Source",
        "body_md": "Body of unknown provenance.",
        "status": "active",
        "created_by": "someone-else",
        "created_at": now,
        "updated_at": now,
    }
    assert db.topic_versions == []

    summary = _run(db, registry_dir)

    assert summary["topics_skipped_human"] == 1
    assert summary["topics_updated"] == 0
    assert summary["topics_created"] == 0
    assert db.topics["top_no_history"]["body_md"] == "Body of unknown provenance."
    assert db.topic_versions == [], "no version row may be appended to a skipped topic"


def test_seed_leaves_an_archived_platform_topic_alone(registry_dir: Path):
    """An archived topic is skipped whole: no rewrite, no edges, no exception.

    create_graph_edge refuses an archived node with a ValueError. Before the
    fix that ValueError escaped the loop and aborted every subsequent run --
    permanently, since the archived topic never goes away on its own.
    """
    db = _seeded_db()
    now = db.next_ts()
    db.topics["top_archived"] = {
        "id": "top_archived",
        "project_id": None,
        "title": "Connector: Demo Source",
        "body_md": "Retired by a curator.",
        "status": "archived",
        "created_by": SEED_ACTOR,
        "created_at": now,
        "updated_at": now,
    }

    summary = _run(db, registry_dir)

    assert summary["topics_skipped_human"] == 1
    assert summary["topics_created"] == 0
    assert summary["topics_updated"] == 0
    assert summary["edges_created"] == 0
    assert summary["modules_skipped"] == [MODULE_NAME]
    assert db.edges == []
    assert db.topics["top_archived"]["body_md"] == "Retired by a curator."


def test_an_archived_twin_does_not_shadow_the_active_topic(registry_dir: Path):
    """Migration 113 allows an archived row beside the live one -- pick the live one.

    The cleanup in 113 archives duplicates rather than deleting them, and the
    partial index only covers non-archived rows. An OLDER archived twin must
    not win the lookup, or the seed would report the module archived forever.
    """
    db = _seeded_db()
    first = _run(db, registry_dir)
    assert first["topics_created"] == 1
    (live_id,) = list(db.topics)

    # Migration 113's cleanup archived an older duplicate of the same title.
    db.topics["top_archived_twin"] = {
        "id": "top_archived_twin",
        "project_id": None,
        "title": "Connector: Demo Source",
        "body_md": "Archived duplicate.",
        "status": "archived",
        "created_by": SEED_ACTOR,
        "created_at": "2020-01-01T00:00:00Z",  # strictly older than the live row
        "updated_at": "2020-01-01T00:00:00Z",
    }

    second = _run(db, registry_dir)

    # The live row is found: no bogus "archived" skip, no duplicate create.
    assert second["topics_created"] == 0
    assert second["topics_skipped_human"] == 0
    assert second["topics_updated"] == 0
    assert second["modules_skipped"] == []
    assert db.topics[live_id]["status"] == "active"


# ---------------------------------------------------------------------------
# Containment: one bad module, concurrent writers
# ---------------------------------------------------------------------------


def test_one_failing_module_does_not_abort_the_run(registry_dir: Path, caplog):
    """The healthy module is still seeded when another one raises."""
    _add_module(registry_dir, "broken-module", "Broken")
    db = _seeded_db()
    db.modules = ["broken-module", MODULE_NAME]  # sorted: broken-module first

    real_create_topic = context_seed.create_topic

    def _explode(conn, **kwargs):
        if kwargs.get("title") == "Connector: Broken":
            raise RuntimeError("registry gremlin")
        return real_create_topic(conn, **kwargs)

    with patch.object(context_seed, "create_topic", _explode):
        summary = _run(db, registry_dir)

    assert summary["topics_created"] == 1
    assert summary["modules"] == [MODULE_NAME]
    assert summary["modules_skipped"] == ["broken-module"]
    assert summary["edges_created"] == 3
    assert any("module_failed" in record.getMessage() for record in caplog.records)


def test_lost_topic_create_race_adopts_the_winner_and_continues(registry_dir: Path):
    """UniqueViolation from migration 113's index is absorbed, not fatal."""
    db = _seeded_db()
    db.topic_race_title = "Connector: Demo Source"

    summary = _run(db, registry_dir)

    # Our INSERT lost; we did not create a topic, and we did not double-write.
    assert summary["topics_created"] == 0
    assert summary["modules"] == []
    assert summary["modules_skipped"] == [MODULE_NAME]
    assert len(db.topics) == 1
    winner_id = next(iter(db.topics))
    assert db.topics[winner_id]["body_md"] == "written by the racing seed"

    # ...and the run carried on: the winner's edges were still created.
    assert summary["edges_created"] == 3
    assert all(edge["from_id"] == winner_id for edge in db.edges)

    # The failed INSERT was contained in a SAVEPOINT that was rolled back.
    assert any(sp.startswith("SAVEPOINT ") for sp in db.savepoints)
    assert any(sp.startswith("ROLLBACK TO SAVEPOINT ") for sp in db.savepoints)


def test_lost_edge_insert_race_skips_that_edge_only(registry_dir: Path):
    """A racing edge insert costs one edge, not the run."""
    db = _seeded_db()
    db.edge_race_remaining = 1

    summary = _run(db, registry_dir)

    assert summary["topics_created"] == 1
    # Three edges attempted, the first lost the race, two were written.
    assert summary["edges_created"] == 2
    assert len(db.edges) == 2
    assert any(sp.startswith("ROLLBACK TO SAVEPOINT ") for sp in db.savepoints)


def test_modules_list_reports_only_written_topics(registry_dir: Path):
    """`modules` = topics this run wrote; a steady-state re-run writes nothing."""
    db = _seeded_db()

    first = _run(db, registry_dir)
    second = _run(db, registry_dir)

    assert first["modules"] == [MODULE_NAME]
    assert first["modules_skipped"] == []
    assert second["modules"] == []
    assert second["modules_skipped"] == []


def test_seed_refreshes_its_own_topic_when_the_registry_changes(registry_dir: Path):
    db = _seeded_db()
    _run(db, registry_dir)
    (topic_id,) = list(db.topics)

    # The registry gains a new field description -> the seed-owned body changes.
    changed = json.loads(json.dumps(MANIFEST))
    changed["source_capabilities"]["fields"].append(
        {
            "field_id": "impressions",
            "kind": "metric",
            "description": "impressions exposed by Demo Source.",
        }
    )
    (registry_dir / MODULE_NAME / "manifest.json").write_text(
        json.dumps(changed), encoding="utf-8"
    )

    summary = _run(db, registry_dir)

    assert summary["topics_updated"] == 1
    assert summary["topics_created"] == 0
    assert "impressions exposed by Demo Source." in db.topics[topic_id]["body_md"]
    latest = db.latest_version(topic_id)
    assert latest["version_number"] == 2
    assert latest["changed_by"] == SEED_ACTOR


# ---------------------------------------------------------------------------
# Hook contract: seeding never breaks its caller
# ---------------------------------------------------------------------------


def test_best_effort_hook_swallows_connection_failure(caplog):
    with patch("core.db.get_connection", side_effect=RuntimeError("db down")):
        result = seed_project_context_best_effort(PROJECT_ID)

    assert result is None
    assert any("seed_failed" in record.getMessage() for record in caplog.records)


def test_best_effort_hook_swallows_seed_failure():
    class _Conn:
        def commit(self) -> None:  # pragma: no cover - never reached
            raise AssertionError("commit must not run after a seed failure")

        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

    with patch("core.db.get_connection", return_value=_Conn()), patch.object(
        context_seed, "seed_project_context", side_effect=ValueError("boom")
    ):
        assert seed_project_context_best_effort(PROJECT_ID) is None


def test_best_effort_hook_is_a_noop_without_project_id():
    with patch("core.db.get_connection", side_effect=AssertionError("must not open")):
        assert seed_project_context_best_effort("") is None


def test_ensure_schema_docs_is_skipped_when_generation_disabled(monkeypatch):
    monkeypatch.delenv("SCHEMA_CONTEXT_ENABLED", raising=False)
    db = FakeDB()

    result = context_seed.ensure_schema_docs(FakeConn(db), PROJECT_ID)

    assert result == {"status": "skipped", "reason": "schema_context_disabled"}
    assert db.calls == []


def test_ensure_schema_docs_delegates_to_the_11_2_generator(monkeypatch):
    monkeypatch.setenv("SCHEMA_CONTEXT_ENABLED", "true")
    db = FakeDB()
    conn = FakeConn(db)
    warehouse = object()
    captured: dict[str, Any] = {}

    def _fake_generate(passed_conn, **kwargs):
        captured["conn"] = passed_conn
        captured.update(kwargs)
        return {"processed": 2, "updated": 1, "skipped": 1, "errors": []}

    with patch("core.schema_context_gen.generate_schema_context", _fake_generate):
        result = context_seed.ensure_schema_docs(
            conn, PROJECT_ID, warehouse_conn=warehouse
        )

    assert result["status"] == "ok"
    assert captured["conn"] is conn
    assert captured["project_id"] == PROJECT_ID
    assert captured["warehouse_conn"] is warehouse
    assert captured["changed_by"] == SEED_ACTOR


def test_ensure_schema_docs_swallows_generator_failure(monkeypatch):
    monkeypatch.setenv("SCHEMA_CONTEXT_ENABLED", "true")

    with patch(
        "core.schema_context_gen.generate_schema_context",
        side_effect=RuntimeError("warehouse gone"),
    ):
        result = context_seed.ensure_schema_docs(
            FakeConn(FakeDB()), PROJECT_ID, warehouse_conn=object()
        )

    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Wiring: both trigger points call the hook (behaviourally, not by grepping)
# ---------------------------------------------------------------------------


def test_best_effort_hook_defaults_to_not_generating_schema_docs():
    """The hook default is ensure_schema=False (decision: hooks never profile).

    Guards the default itself: a future edit that flips it back to True would
    reintroduce a full-warehouse DuckDB pass on every pull.
    """
    captured: dict[str, Any] = {}

    class _Conn:
        def commit(self) -> None:
            captured["committed"] = True

        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

    def _fake_seed(conn, project_id, **kwargs):
        captured.update(kwargs)
        return {
            "topics_created": 0,
            "edges_created": 0,
            "topics_skipped_human": 0,
            "modules": [],
            "modules_skipped": [],
        }

    with patch("core.db.get_connection", return_value=_Conn()), patch.object(
        context_seed, "seed_project_context", _fake_seed
    ):
        seed_project_context_best_effort(PROJECT_ID)

    assert captured["ensure_schema"] is False
    assert captured["module_names"] is None
    assert captured["committed"] is True


def test_landing_hook_invokes_the_seed_with_the_landed_module():
    """The queue's success path really calls the seed with (project, module).

    Drives the actual worker entrypoint (``_execute_job``) with a fake
    connection, exactly like server/tests/core/test_queue.py, and asserts the
    hook call -- a source grep proved only that a string was present.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    job = {
        "id": "job_seed_hook",
        "pull_id": "pull_seed_hook",
        "connection_ref_id": "conn_ref_seed",
        "date_from": "2026-07-01",
        "date_to": "2026-07-02",
        "requested_by": "tester@example.com",
        "attempt_count": 0,
    }
    conn_ref_row = ("conn_ref_seed", "nango-conn-seed", "demo-source", "proj-seed-01")

    class _Cursor:
        description = [("id",), ("nango_connection_id",), ("provider",), ("project_id",)]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            return None

        def fetchone(self):
            return conn_ref_row

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            return None

        def close(self):
            return None

    from contextlib import contextmanager as _contextmanager  # noqa: PLC0415

    @_contextmanager
    def _fake_get_connection():
        yield _Conn()

    seed_spy = MagicMock(return_value=None)
    pull_fn = MagicMock(return_value={"row_count": 3})

    with patch("core.db.get_connection", new=_fake_get_connection), patch(
        "core.main.get_module_pull_fn", return_value=pull_fn
    ), patch("core.audit.write_audit_row", MagicMock()), patch.object(
        context_seed, "seed_project_context_best_effort", seed_spy
    ):
        from core.queue import _execute_job  # noqa: PLC0415

        _execute_job(job)

    seed_spy.assert_called_once_with(
        "proj-seed-01", module_names=["demo-source"], ensure_schema=False
    )


# ---------------------------------------------------------------------------
# pg-gated end-to-end seed (TEST_POSTGRES_DSN)
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        return False
    try:
        import psycopg  # noqa: PLC0415

        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(
    not _pg_reachable(), reason="TEST_POSTGRES_DSN not set/reachable -- skip live PG"
)

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "nango" / "migrations"
_MIGRATION_031 = _MIGRATIONS_DIR / "031_context_layer.sql"
_MIGRATION_113 = _MIGRATIONS_DIR / "113_context_topics_platform_title_unique.sql"


@pg_available
def test_live_seed_is_idempotent_and_respects_the_human_edit(
    registry_dir: Path, monkeypatch
):
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])
    from core.context_store import update_topic  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    project_id = f"proj_seed_{uuid.uuid4().hex[:10]}"
    title = "Connector: Demo Source"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_MIGRATION_031.read_text(encoding="utf-8"))
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(_MIGRATION_113.read_text(encoding="utf-8"))
        conn.commit()

        # Two schema docs on one of the module's relations, one unrelated doc.
        with conn.cursor() as cur:
            for suffix, relation, doc_kind in (
                ("a", "stg_demo_source_daily", "columns"),
                ("b", "stg_demo_source_daily", "description"),
                ("c", "stg_unrelated_daily", "columns"),
            ):
                cur.execute(
                    """
                    INSERT INTO app.schema_context
                        (id, project_id, relation, doc_kind, body_md, generated_at)
                    VALUES (%s, %s, %s, %s, %s, now())
                    """,
                    (f"sctx_{project_id}_{suffix}", project_id, relation, doc_kind, "# doc"),
                )
        conn.commit()

        try:
            first = seed_project_context(
                conn,
                project_id,
                module_names=[MODULE_NAME],
                modules_dir=registry_dir,
                ensure_schema=False,
            )
            conn.commit()
            second = seed_project_context(
                conn,
                project_id,
                module_names=[MODULE_NAME],
                modules_dir=registry_dir,
                ensure_schema=False,
            )
            conn.commit()

            assert first["topics_created"] == 1
            assert first["edges_created"] == 2
            assert second["topics_created"] == 0
            assert second["edges_created"] == 0

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, created_by FROM app.context_topics "
                    "WHERE project_id IS NULL AND title = %s",
                    (title,),
                )
                topic_rows = cur.fetchall()
                assert len(topic_rows) == 1
                topic_id, created_by = topic_rows[0]
                assert created_by == SEED_ACTOR

                cur.execute(
                    "SELECT count(*) FROM app.context_graph "
                    "WHERE from_id = %s AND edge_type = 'describes'",
                    (topic_id,),
                )
                assert cur.fetchone()[0] == 2

            # A curator edits the seeded topic -> the seed must not touch it.
            update_topic(
                conn,
                topic_id=topic_id,
                patch={"body_md": "Curated by a human."},
                changed_by="curator@example.com",
            )
            conn.commit()

            third = seed_project_context(
                conn,
                project_id,
                module_names=[MODULE_NAME],
                modules_dir=registry_dir,
                ensure_schema=False,
            )
            conn.commit()

            assert third["topics_skipped_human"] == 1
            assert third["topics_updated"] == 0
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT body_md FROM app.context_topics WHERE id = %s", (topic_id,)
                )
                assert cur.fetchone()[0] == "Curated by a human."

            # Migration 113: the title/platform key is a DB invariant now, not
            # just a convention in context_seed. A second row with the same
            # platform title must be impossible even outside the seed.
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT sp_dupe")
                with pytest.raises(Exception) as excinfo:
                    cur.execute(
                        """
                        INSERT INTO app.context_topics
                            (id, project_id, title, body_md, status, created_by)
                        VALUES (%s, NULL, %s, '', 'active', 'someone-else')
                        """,
                        (f"top_dupe_{uuid.uuid4().hex[:8]}", title),
                    )
                assert "uq_context_topics_title_platform" in str(excinfo.value)
                cur.execute("ROLLBACK TO SAVEPOINT sp_dupe")
            conn.commit()
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.context_graph WHERE project_id = %s", (project_id,)
                )
                cur.execute(
                    "DELETE FROM app.schema_context WHERE project_id = %s", (project_id,)
                )
                # app.context_topics_versions is append-only (migration 031
                # installs a BEFORE DELETE trigger that raises), so the history
                # rows deliberately stay behind -- they are orphan-safe, the
                # table has no FK to app.context_topics.
                cur.execute(
                    "DELETE FROM app.context_topics "
                    "WHERE project_id IS NULL AND title = %s",
                    (title,),
                )
            conn.commit()


def test_seed_new_project_helper_calls_hook_without_schema_generation():
    """Non-pg proof of the _create_project wiring (44.2 re-review).

    The pg-gated end-to-end test below skips without TEST_POSTGRES_DSN, which
    would leave the project-creation call site unproven on the default local
    run. _seed_new_project is the extracted 5-line hook block _create_project
    calls unconditionally after commit -- asserting on the helper pins both
    the invocation and its ensure_schema=False argument without a database.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from core.admin_api import _seed_new_project  # noqa: PLC0415

    seed_spy = MagicMock(return_value=None)
    with patch.object(context_seed, "seed_project_context_best_effort", seed_spy):
        _seed_new_project("proj-helper-01")

    seed_spy.assert_called_once_with("proj-helper-01", ensure_schema=False)


def test_seed_new_project_helper_swallows_seed_failure():
    """A raising seed never propagates out of the project-creation hook."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    from core.admin_api import _seed_new_project  # noqa: PLC0415

    seed_spy = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(context_seed, "seed_project_context_best_effort", seed_spy):
        _seed_new_project("proj-helper-02")  # must not raise

    seed_spy.assert_called_once()


@pg_available
@pytest.mark.anyio
async def test_project_creation_invokes_the_seed_without_schema_generation(
    tmp_path, monkeypatch
):
    """POST /api/projects really calls the hook -- and never asks for profiling.

    Behavioural replacement for the old source-grep wiring assertion: the
    handler is driven end to end (same pattern as
    server/tests/core/test_revocation.py) with the hook patched, so both the
    call AND its ensure_schema=False argument are proven.
    """
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])
    from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

    from core.admin_api import _create_project  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    seed_spy = MagicMock(return_value=None)
    slug = f"seedhook-{uuid.uuid4().hex[:8]}"

    with patch(
        "core.admin_api._check_auth", return_value=(True, "tester@example.com")
    ), patch.dict(
        os.environ,
        {"TENANT_KEY_DIR": str(tmp_path / "keys"), "TENANT_KEY_BACKEND": "local"},
    ), patch.object(context_seed, "seed_project_context_best_effort", seed_spy):
        request = MagicMock()
        request.body = AsyncMock(
            return_value=json.dumps({"name": f"Seed hook {slug}", "slug": slug}).encode()
        )
        response = await _create_project(request)

    assert response.status_code == 201, response.body
    project_id = json.loads(response.body)["id"]

    try:
        seed_spy.assert_called_once_with(project_id, ensure_schema=False)
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for statement in (
                    "DELETE FROM app.project_preferences WHERE project_id = %s",
                    "DELETE FROM app.tenant_key_audit WHERE project_id = %s",
                    "DELETE FROM app.projects WHERE id = %s",
                ):
                    cur.execute("SAVEPOINT sp_seed_cleanup")
                    try:
                        cur.execute(statement, (project_id,))
                    except Exception:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_seed_cleanup")
                    cur.execute("RELEASE SAVEPOINT sp_seed_cleanup")
            conn.commit()
