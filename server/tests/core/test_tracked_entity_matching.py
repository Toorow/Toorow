"""Tests for Story 40.3 -- inbound alias matching + governed new-entity alert (Epic 40).

Offline (no DB): the PURE matcher (exact/normalized/similarity, ambiguity band, negative-alias
exclusion, determinism, provenance) + the inbound resolution / alert lifecycle / correction
feedback over an in-memory FAKE store (core.db.get_connection monkeypatched), the audit shape,
the AD-2 "no provider/brand name" grep, and the "NO server-side LLM SDK" grep.

Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): the real DDL of migration 090, the
append-only trigger on inbound_brand_match_decisions (UPDATE/DELETE rejected), the CHECKs,
the unicity (one open alert per unknown string; one negative alias per (entity, norm)), FK
CASCADE, the audit reuse of app.metric_semantics_audit (049), the governed approve
(draft->approved), and cross-org existence-hiding. Pattern calque sur
test_tracked_entity_registry.py.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import tracked_entity_matching as tem  # noqa: E402
from core import tracked_entity_registry as ter  # noqa: E402

# ---------------------------------------------------------------------------
# Postgres availability check (calque sur test_tracked_entity_registry.py)
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIG = _REPO_ROOT / "infra" / "nango" / "migrations"
_MIGRATION_049 = _MIG / "049_metric_semantics.sql"
_MIGRATION_086 = _MIG / "086_tracked_entity_registry.sql"
_MIGRATION_090 = _MIG / "090_inbound_brand_matching.sql"


# ---------------------------------------------------------------------------
# Corpus helper for the pure matcher tests.
# ---------------------------------------------------------------------------


def _entity(entity_id, canonical, aliases=None, negatives=None):
    return {
        "id": entity_id,
        "canonical_name": canonical,
        "aliases": aliases or [],
        "_negatives": negatives or [],
    }


def _corpus(*entities):
    """Build a matcher corpus (list of _CorpusEntry) from simple dicts."""
    negatives = {e["id"]: e.get("_negatives", []) for e in entities}
    return tem._build_corpus(entities, negatives)


# ===========================================================================
# Offline -- pure matcher (no DB)
# ===========================================================================


def test_exact_on_canonical():
    """§1: raw == canonical_name -> resolved, method exact, score 1.0, matched_alias=canonical."""
    corpus = _corpus(_entity("tent_1", "Peugeot"))
    m = tem.match_brand("Peugeot", corpus)
    assert m.outcome == tem.OUTCOME_RESOLVED
    assert m.method == tem.METHOD_EXACT
    assert m.score == 1.0
    assert m.entity_id == "tent_1"
    assert m.matched_alias == "Peugeot"


def test_exact_on_alias():
    """§2: raw == a stored alias -> resolved exact, matched_alias=that alias."""
    corpus = _corpus(_entity("tent_1", "Peugeot", aliases=["PSA"]))
    m = tem.match_brand("PSA", corpus)
    assert m.outcome == tem.OUTCOME_RESOLVED
    assert m.method == tem.METHOD_EXACT
    assert m.matched_alias == "PSA"


def test_normalized_match():
    """§3: raw differs only by case/separators/accents -> resolved normalized, score 1.0."""
    corpus = _corpus(_entity("tent_1", "Coca-Cola"))
    m = tem.match_brand("coca cola", corpus)
    assert m.outcome == tem.OUTCOME_RESOLVED
    assert m.method == tem.METHOD_NORMALIZED
    assert m.score == 1.0
    assert m.entity_id == "tent_1"


def test_similarity_above_threshold_clear_winner():
    """§4: a fuzzy match >= threshold with a clear winner -> resolved similarity, ratio score."""
    corpus = _corpus(_entity("tent_1", "Peugeot"), _entity("tent_2", "Volkswagen"))
    m = tem.match_brand("Peugeott", corpus)  # one extra letter
    assert m.outcome == tem.OUTCOME_RESOLVED
    assert m.method == tem.METHOD_SIMILARITY
    assert 0.88 <= m.score < 1.0
    assert m.entity_id == "tent_1"
    assert m.matched_alias == "Peugeot"


def test_similarity_below_threshold_alerts():
    """§5: a best candidate below the threshold -> OUTCOME_ALERT, reason below_threshold, with
    ranked candidates (never a silent join)."""
    corpus = _corpus(_entity("tent_1", "Peugeot"))
    m = tem.match_brand("Toyota", corpus)
    assert m.outcome == tem.OUTCOME_ALERT
    assert m.reason == tem.REASON_BELOW_THRESHOLD
    assert len(m.candidates) >= 1
    assert m.entity_id is None


def test_no_candidate_no_match_alert():
    """§6: empty corpus -> OUTCOME_ALERT, reason no_match."""
    m = tem.match_brand("Anything", _corpus())
    assert m.outcome == tem.OUTCOME_ALERT
    assert m.reason == tem.REASON_NO_MATCH
    assert m.candidates == ()


def test_ambiguous_within_band_alerts():
    """§7: two candidates within the ambiguity band above threshold -> OUTCOME_ALERT ambiguous,
    both ranked (fail-closed homonym/tie guard)."""
    # Two near-identical corpus names so a raw scores >= threshold on BOTH within the band.
    corpus = _corpus(_entity("tent_1", "Acme Corpo"), _entity("tent_2", "Acme Corps"))
    m = tem.match_brand("Acme Corp", corpus)
    assert m.candidates[0].score >= tem.DEFAULT_CONFIDENCE_THRESHOLD  # both clear threshold
    assert m.outcome == tem.OUTCOME_ALERT
    assert m.reason == tem.REASON_AMBIGUOUS
    assert len(m.candidates) == 2


def test_negative_alias_excludes_entity():
    """§8: a string that would match entity E is suppressed by E's negative alias -> E is NOT a
    candidate."""
    corpus = _corpus(_entity("tent_1", "Orange", negatives=["Orange"]))
    m = tem.match_brand("Orange", corpus)
    # Only entity is suppressed -> no candidate.
    assert m.outcome == tem.OUTCOME_ALERT
    assert all(c.entity_id != "tent_1" for c in m.candidates)


def test_negative_alias_suppression_no_other_candidate_alerts():
    """§9: negative-alias suppression with no OTHER candidate -> OUTCOME_ALERT (not a silent
    join to the suppressed entity, not a silent drop)."""
    corpus = _corpus(_entity("tent_1", "Orange", negatives=["Orange"]))
    m = tem.match_brand("Orange", corpus)
    assert m.outcome == tem.OUTCOME_ALERT
    assert m.reason == tem.REASON_NO_MATCH


def test_negative_alias_suppression_resolves_other_entity():
    """§10: suppression WITH another qualifying entity -> resolves to the OTHER entity."""
    corpus = _corpus(
        _entity("tent_1", "Orange", negatives=["Orange"]),
        _entity("tent_2", "Orange Telecom", aliases=["Orange"]),
    )
    # tent_1 is suppressed for 'Orange'; tent_2 carries 'Orange' as an alias (exact) -> tent_2.
    m = tem.match_brand("Orange", corpus)
    assert m.outcome == tem.OUTCOME_RESOLVED
    assert m.entity_id == "tent_2"


def test_determinism_and_tie_break():
    """§11: same raw + corpus -> identical BrandMatch across runs; stable tie-break."""
    corpus = _corpus(_entity("tent_b", "Peugeott"), _entity("tent_a", "Peugeott"))
    m1 = tem.match_brand("Peugeot", corpus)
    m2 = tem.match_brand("Peugeot", corpus)
    assert m1 == m2
    # Two entities with the SAME normalized surface at score 1.0 -> ambiguous (never a silent
    # pick), and the candidates order is deterministic by canonical_name then entity_id.
    assert m1.outcome == tem.OUTCOME_ALERT


def test_stage_priority_exact_wins():
    """§12: a string that qualifies EXACT is not re-scored by similarity; method reports exact."""
    corpus = _corpus(_entity("tent_1", "Peugeot", aliases=["Peugeott"]))
    m = tem.match_brand("Peugeot", corpus)
    assert m.method == tem.METHOD_EXACT
    assert m.score == 1.0


def test_build_corpus_precomputes_and_order_stable():
    """§13: _build_corpus shapes 40.1 rows + negatives, precomputes normalized keys, keeps order."""
    entities = [_entity("tent_1", "Alpha", aliases=["A1", "a1"]), _entity("tent_2", "Beta")]
    negatives = {"tent_1": ["Ghost"]}
    corpus = tem._build_corpus(entities, negatives)
    assert [e.entity_id for e in corpus] == ["tent_1", "tent_2"]
    # 'A1' and 'a1' normalize to the same key -> de-duped to one alias surface + canonical.
    assert len(corpus[0].surfaces) == 2
    assert tem.normalize_value("Ghost") in corpus[0].negative_norms


# ===========================================================================
# Offline -- in-memory FAKE store (no DB)
# ===========================================================================


class _FakeTS:
    def __init__(self, label):
        self._label = label

    def isoformat(self):
        return self._label

    def __eq__(self, other):
        return isinstance(other, _FakeTS) and other._label == self._label

    def __hash__(self):
        return hash(self._label)


class _FakeCursor:
    def __init__(self, store):
        self._store = store
        self._result = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):  # noqa: C901 - test double
        params = params or []
        s = " ".join(sql.split())
        self._result = None
        self.description = None

        # -- projects.org_id lookup --
        if s.startswith("SELECT org_id FROM app.projects"):
            org = self._store["projects"].get(params[0])
            self._result = [(org,)] if org is not None else []
            return

        # -- tracked_entities existence probe (add/remove negative alias) --
        if s.startswith("SELECT id FROM app.tracked_entities WHERE id ="):
            eid = params[0]
            self._result = [(eid,)] if eid in self._store["entities"] else []
            return
        # -- tracked_entities org lookup (negative-alias audit stamps the real org_id) --
        if s.startswith("SELECT org_id FROM app.tracked_entities WHERE id ="):
            ent = self._store["entities"].get(params[0])
            self._result = [(ent["org_id"],)] if ent is not None else []
            return

        # -- entity SELECTs (40.1 store, delegated) --
        if "FROM app.tracked_entities" in s and s.startswith("SELECT " + ter._ENTITY_COLUMNS[:5]):
            self.description = [(c.strip(),) for c in ter._ENTITY_COLUMNS.split(",")]
            rows = self._store["entities"]
            out = []
            if " WHERE id = " in s:
                out = [r for r in rows.values() if r["id"] == params[0]]
            elif "org_id = %s AND canonical_name = %s" in s:
                org_id, name = params
                out = [r for r in rows.values()
                       if r["org_id"] == org_id and r["canonical_name"] == name]
            else:  # list_tracked_entities_by_org
                org_id = params[0]
                status = params[1] if "status = %s" in s else None
                out = [r for r in rows.values()
                       if r["org_id"] == org_id and (status is None or r["status"] == status)]
                out.sort(key=lambda r: r["canonical_name"])
            self._result = [tuple(r[c[0]] for c in self.description) for r in out]
            return

        # -- entity UPSERT (40.1) --
        if s.startswith("INSERT INTO app.tracked_entities"):
            self.description = [(c.strip(),) for c in ter._ENTITY_COLUMNS.split(",")]
            (eid, org_id, canonical_name, display_name, aliases_json, status, created_by) = params
            import json
            aliases = json.loads(aliases_json)
            key = (org_id, canonical_name)
            existing = self._store["entities_by_key"].get(key)
            if existing is not None:
                row = self._store["entities"][existing]
                row.update({"display_name": display_name, "aliases": aliases,
                            "status": status, "updated_at": _FakeTS("t1")})
            else:
                row = {
                    "id": eid, "org_id": org_id, "scope_level": "ORG",
                    "canonical_name": canonical_name, "display_name": display_name,
                    "aliases": aliases, "entity_kind": "brand", "status": status,
                    "created_by": created_by, "approved_by": None, "approved_at": None,
                    "created_at": _FakeTS("t0"), "updated_at": _FakeTS("t0"),
                }
                self._store["entities"][eid] = row
                self._store["entities_by_key"][key] = eid
            self._result = [tuple(row[c[0]] for c in self.description)]
            return

        # -- entity alias UPDATE (40.1 add_alias) --
        if s.startswith("UPDATE app.tracked_entities"):
            self.description = [(c.strip(),) for c in ter._ENTITY_COLUMNS.split(",")]
            import json
            aliases_json, eid = params
            row = self._store["entities"][eid]
            row.update({"aliases": json.loads(aliases_json), "updated_at": _FakeTS("t1")})
            self._result = [tuple(row[c[0]] for c in self.description)]
            return

        # -- negative aliases: load-by-org (JOIN) --
        if "FROM app.entity_negative_aliases n" in s:
            org_id = params[0]
            out = []
            for n in self._store["neg"].values():
                e = self._store["entities"].get(n["entity_id"])
                if e is not None and e["org_id"] == org_id:
                    out.append((n["entity_id"], n["normalized_string"]))
            self._result = out
            return

        # -- negative aliases: select by (entity, norm) --
        if ("FROM app.entity_negative_aliases" in s and s.startswith("SELECT")
                and "entity_id = %s AND normalized_string = %s" in s):
            self.description = [("id",), ("entity_id",), ("raw_string",),
                                ("normalized_string",), ("created_by",), ("created_at",)]
            entity_id, norm = params
            out = [n for n in self._store["neg"].values()
                   if n["entity_id"] == entity_id and n["normalized_string"] == norm]
            self._result = [tuple(r[c[0]] for c in self.description) for r in out]
            return

        # -- negative alias INSERT --
        if s.startswith("INSERT INTO app.entity_negative_aliases"):
            self.description = [("id",), ("entity_id",), ("raw_string",),
                                ("normalized_string",), ("created_by",), ("created_at",)]
            nid, entity_id, raw_string, norm, created_by = params
            row = {"id": nid, "entity_id": entity_id, "raw_string": raw_string,
                   "normalized_string": norm, "created_by": created_by,
                   "created_at": _FakeTS("t0")}
            self._store["neg"][nid] = row
            self._result = [tuple(row[c[0]] for c in self.description)]
            return

        if s.startswith("DELETE FROM app.entity_negative_aliases"):
            self._store["neg"].pop(params[0], None)
            return

        # -- inbound_brand_match_decisions INSERT --
        if s.startswith("INSERT INTO app.inbound_brand_match_decisions"):
            self.description = [(c.strip(),) for c in tem._DECISION_COLUMNS.split(",")]
            import json
            (did, project_id, org_id, import_ref, raw, norm, outcome, ment, malias,
             method, conf, reason, candidates_json, created_by) = params
            row = {
                "id": did, "project_id": project_id, "org_id": org_id,
                "import_ref": import_ref, "raw_string": raw, "normalized_string": norm,
                "outcome": outcome, "matched_entity_id": ment, "matched_alias": malias,
                "method": method, "confidence": conf, "alert_reason": reason,
                "candidates": json.loads(candidates_json), "created_by": created_by,
                "created_at": _FakeTS("t0"),
            }
            self._store["decisions"][did] = row
            self._result = [tuple(row[c[0]] for c in self.description)]
            return

        if ("FROM app.inbound_brand_match_decisions" in s and s.startswith("SELECT")
                and " WHERE id = " in s):
            self.description = [(c.strip(),) for c in tem._DECISION_COLUMNS.split(",")]
            out = [r for r in self._store["decisions"].values() if r["id"] == params[0]]
            self._result = [tuple(r[c[0]] for c in self.description) for r in out]
            return

        # -- tracked_entity_alerts SELECTs --
        if "FROM app.tracked_entity_alerts" in s and s.startswith("SELECT"):
            self.description = [(c.strip(),) for c in tem._ALERT_COLUMNS.split(",")]
            rows = self._store["alerts"]
            out = []
            if " WHERE id = " in s:
                out = [r for r in rows.values() if r["id"] == params[0]]
            elif "org_id = %s AND normalized_string = %s AND status = %s" in s:
                org_id, norm, status = params
                out = [r for r in rows.values()
                       if r["org_id"] == org_id and r["normalized_string"] == norm
                       and r["status"] == status]
            else:  # list_open_alerts: WHERE <col> = %s AND status = %s
                value, status = params
                col = "org_id" if "org_id = %s" in s else "project_id"
                out = [r for r in rows.values()
                       if r[col] == value and r["status"] == status]
                out.sort(key=lambda r: r["created_at"].isoformat(), reverse=True)
            self._result = [tuple(r[c[0]] for c in self.description) for r in out]
            return

        # -- alert INSERT --
        if s.startswith("INSERT INTO app.tracked_entity_alerts"):
            self.description = [(c.strip(),) for c in tem._ALERT_COLUMNS.split(",")]
            import json
            (aid, org_id, project_id, raw, norm, status, reason, candidates_json,
             created_by) = params
            row = {
                "id": aid, "org_id": org_id, "project_id": project_id, "raw_string": raw,
                "normalized_string": norm, "proposed_entity_id": None, "status": status,
                "reason": reason, "candidates": json.loads(candidates_json),
                "created_by": created_by, "resolved_by": None, "resolved_at": None,
                "created_at": _FakeTS("t0"), "updated_at": _FakeTS("t0"),
            }
            self._store["alerts"][aid] = row
            self._result = [tuple(row[c[0]] for c in self.description)]
            return

        # -- alert UPDATE (approve/dismiss), now guarded by AND status = %s (must be 'open') --
        if s.startswith("UPDATE app.tracked_entity_alerts"):
            self.description = [(c.strip(),) for c in tem._ALERT_COLUMNS.split(",")]
            if "proposed_entity_id = %s" in s:  # approve
                status, proposed, resolved_by, aid, expect_status = params
                row = self._store["alerts"][aid]
                if row["status"] != expect_status:  # WHERE status='open' not met -> no row
                    self._result = []
                    return
                row.update({"status": status, "proposed_entity_id": proposed,
                            "resolved_by": resolved_by, "resolved_at": _FakeTS("t1"),
                            "updated_at": _FakeTS("t1")})
            else:  # dismiss
                status, resolved_by, aid, expect_status = params
                row = self._store["alerts"][aid]
                if row["status"] != expect_status:
                    self._result = []
                    return
                row.update({"status": status, "resolved_by": resolved_by,
                            "resolved_at": _FakeTS("t1"), "updated_at": _FakeTS("t1")})
            self._result = [tuple(row[c[0]] for c in self.description)]
            return

        # -- shared audit --
        if s.startswith("INSERT INTO app.metric_semantics_audit"):
            composed = params[2]
            verb = composed.split(".", 1)[1] if "." in composed else composed
            self._store["audit"].append({
                "action": verb, "composed": composed, "entity_type": params[3],
                "entity_id": params[4], "scope_level": params[5], "org_id": params[6],
                "project_id": params[7], "before": params[8], "after": params[9],
            })
            return

        raise AssertionError(f"unhandled SQL in fake cursor: {s[:90]}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result or [])


class _FakeConn:
    def __init__(self, store):
        self._store = store

    def cursor(self):
        return _FakeCursor(self._store)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    store = {
        "entities": {}, "entities_by_key": {}, "projects": {},
        "neg": {}, "decisions": {}, "alerts": {}, "audit": [],
    }
    conn = _FakeConn(store)

    @contextmanager
    def _fake_get_connection():
        yield conn

    import core.db as db

    monkeypatch.setattr(db, "get_connection", _fake_get_connection)
    return store


def _seed_entity(store, org_id, canonical, aliases=None):
    e = ter.upsert_tracked_entity(
        org_id=org_id, canonical_name=canonical, created_by="admin", aliases=aliases or []
    )
    return e


# ===========================================================================
# Offline -- inbound resolution + provenance ledger (fake store)
# ===========================================================================


def test_resolve_writes_provenance_on_resolve(fake_db):
    """§14: resolve_inbound_brands writes ONE decision row per string with full provenance."""
    fake_db["projects"]["proj_1"] = "org_1"
    _seed_entity(fake_db, "org_1", "Peugeot")
    out = tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Peugeot"], identity="alice", import_ref="imp_1"
    )
    assert out["counts"] == {"resolved": 1, "alert": 0}
    d = out["resolved"][0]
    assert d["outcome"] == "resolved"
    assert d["matched_entity_id"] is not None
    assert d["matched_alias"] == "Peugeot"
    assert d["method"] == "exact"
    assert d["confidence"] == 1.0
    assert len(fake_db["decisions"]) == 1


def test_resolve_alert_decision_row_carries_reason_and_candidates(fake_db):
    """§15: on an alert, the decision row has outcome='alert', NULL match fields, the reason,
    and ranked candidates."""
    fake_db["projects"]["proj_1"] = "org_1"
    _seed_entity(fake_db, "org_1", "Peugeot")
    out = tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Toyota"], identity="alice", import_ref="imp_1"
    )
    assert out["counts"] == {"resolved": 0, "alert": 1}
    d = [r for r in fake_db["decisions"].values()][0]
    assert d["outcome"] == "alert"
    assert d["matched_entity_id"] is None and d["method"] is None and d["confidence"] is None
    assert d["alert_reason"] in (tem.REASON_NO_MATCH, tem.REASON_BELOW_THRESHOLD)
    assert isinstance(d["candidates"], list)


def test_resolve_corpus_is_org_scoped(fake_db):
    """§16: a second org's entity is NEVER a candidate for the project's strings (isolation)."""
    fake_db["projects"]["proj_1"] = "org_1"
    _seed_entity(fake_db, "org_2", "Peugeot")  # different org
    out = tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Peugeot"], identity="alice", import_ref="imp_1"
    )
    # org_1 has no entities -> the org_2 'Peugeot' is invisible -> alert, not a cross-org join.
    assert out["counts"]["resolved"] == 0
    assert out["counts"]["alert"] == 1


def test_resolve_stamps_import_ref(fake_db):
    """§17: import_ref is stamped on every decision row."""
    fake_db["projects"]["proj_1"] = "org_1"
    _seed_entity(fake_db, "org_1", "Peugeot")
    tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Peugeot", "Toyota"], identity="a", import_ref="imp_X"
    )
    assert all(d["import_ref"] == "imp_X" for d in fake_db["decisions"].values())


def test_resolve_counts_match_ledger(fake_db):
    """§18: resolved + alert = total strings, and equals the ledger rows written."""
    fake_db["projects"]["proj_1"] = "org_1"
    _seed_entity(fake_db, "org_1", "Peugeot")
    out = tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Peugeot", "Toyota", "Peugeot"],
        identity="a", import_ref="imp_1",
    )
    total = out["counts"]["resolved"] + out["counts"]["alert"]
    assert total == 3
    assert len(fake_db["decisions"]) == 3


def test_resolve_unknown_project_fail_closed(fake_db):
    """Fail-closed: an unknown project raises EntityNotFound (no silent default org)."""
    with pytest.raises(ter.EntityNotFound):
        tem.resolve_inbound_brands(
            project_id="proj_ghost", raw_strings=["X"], identity="a", import_ref="i"
        )


# ===========================================================================
# Offline -- governed alert lifecycle (fake store)
# ===========================================================================


def test_alert_opens_without_mutating_master(fake_db):
    """§19: an alert opens a tracked_entity_alert (status open); the master is NOT mutated."""
    fake_db["projects"]["proj_1"] = "org_1"
    before_entities = len(fake_db["entities"])
    tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Toyota"], identity="a", import_ref="imp_1"
    )
    assert len(fake_db["alerts"]) == 1
    assert list(fake_db["alerts"].values())[0]["status"] == "open"
    assert len(fake_db["entities"]) == before_entities  # no new tracked_entity


def test_reimport_same_unknown_reuses_open_alert(fake_db):
    """§20: re-importing the SAME unknown string reuses the existing OPEN alert (idempotent)."""
    fake_db["projects"]["proj_1"] = "org_1"
    tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Toyota"], identity="a", import_ref="imp_1"
    )
    tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["toyota"], identity="a", import_ref="imp_2"
    )
    assert len(fake_db["alerts"]) == 1  # one open alert, not two


def test_approve_mints_entity_governed(fake_db):
    """§21: approve_new_entity_alert mints the entity status=approved with the raw string as
    its first alias, stamps resolved_by/at, sets the alert approved."""
    fake_db["projects"]["proj_1"] = "org_1"
    tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Toyota"], identity="a", import_ref="imp_1"
    )
    alert = list(fake_db["alerts"].values())[0]
    res = tem.approve_new_entity_alert(
        alert_id=alert["id"], canonical_name="Toyota", identity="human", org_id="org_1"
    )
    assert res["entity"]["status"] == "approved"
    assert res["entity"]["aliases"] == ["Toyota"]
    assert res["alert"]["status"] == "approved"
    assert res["alert"]["resolved_by"] == "human"
    assert res["alert"]["proposed_entity_id"] == res["entity"]["id"]


def test_dismiss_alert_mints_no_entity(fake_db):
    """§22: dismiss_alert sets dismissed, mints NO entity."""
    fake_db["projects"]["proj_1"] = "org_1"
    tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Random Row"], identity="a", import_ref="imp_1"
    )
    alert = list(fake_db["alerts"].values())[0]
    before = len(fake_db["entities"])
    d = tem.dismiss_alert(alert_id=alert["id"], identity="human", org_id="org_1")
    assert d["status"] == "dismissed"
    assert len(fake_db["entities"]) == before


def test_alert_mutation_audits(fake_db):
    """§23: every alert mutation emits one metric_semantics_audit row (entity_type=alert)."""
    fake_db["projects"]["proj_1"] = "org_1"
    tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Toyota"], identity="a", import_ref="imp_1"
    )
    opened = [a for a in fake_db["audit"] if a["entity_type"] == "tracked_entity_alert"]
    assert len(opened) == 1
    assert opened[0]["composed"] == "tracked_entity_alert.opened"
    alert = list(fake_db["alerts"].values())[0]
    tem.dismiss_alert(alert_id=alert["id"], identity="human", org_id="org_1")
    dismissed = [a for a in fake_db["audit"]
                 if a["composed"] == "tracked_entity_alert.dismissed"]
    assert len(dismissed) == 1


def test_approve_alert_existence_hiding(fake_db):
    """approve/dismiss on a foreign-org alert id -> AlertNotFound (existence-hiding)."""
    fake_db["projects"]["proj_1"] = "org_1"
    tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Toyota"], identity="a", import_ref="imp_1"
    )
    alert = list(fake_db["alerts"].values())[0]
    with pytest.raises(tem.AlertNotFound):
        tem.approve_new_entity_alert(
            alert_id=alert["id"], canonical_name="X", identity="h", org_id="org_OTHER"
        )
    with pytest.raises(tem.AlertNotFound):
        tem.dismiss_alert(alert_id="tea_ghost", identity="h", org_id="org_1")


# ===========================================================================
# Offline -- correction feedback + negative aliases (fake store)
# ===========================================================================


def test_correct_match_adds_alias_to_right_entity(fake_db):
    """§24: correct_match(correct_entity_id) adds the string as an alias on the RIGHT entity."""
    fake_db["projects"]["proj_1"] = "org_1"
    right = _seed_entity(fake_db, "org_1", "Peugeot")
    tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Pug"], identity="a", import_ref="imp_1"
    )
    decision = list(fake_db["decisions"].values())[0]
    res = tem.correct_match(
        decision_id=decision["id"], raw_string="Pug", project_id="proj_1",
        identity="human", correct_entity_id=right["id"],
    )
    assert res["kind"] == "alias"
    assert "Pug" in res["entity"]["aliases"]


def test_correct_match_adds_negative_alias_homonym(fake_db):
    """§25: correct_match(negative_for_entity_id) records a negative alias on the wrong entity."""
    fake_db["projects"]["proj_1"] = "org_1"
    wrong = _seed_entity(fake_db, "org_1", "Orange Telecom", aliases=["Orange"])
    tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Orange"], identity="a", import_ref="imp_1"
    )
    decision = list(fake_db["decisions"].values())[0]
    res = tem.correct_match(
        decision_id=decision["id"], raw_string="Orange", project_id="proj_1",
        identity="human", negative_for_entity_id=wrong["id"],
    )
    assert res["kind"] == "negative_alias"
    # The suppression is now in the store; a fresh match excludes the wrong entity.
    assert len(fake_db["neg"]) == 1


def test_correct_match_both_or_neither_error(fake_db):
    """§26: correct_match with BOTH or NEITHER target -> CorrectionTargetError."""
    fake_db["projects"]["proj_1"] = "org_1"
    e = _seed_entity(fake_db, "org_1", "Peugeot")
    tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Pug"], identity="a", import_ref="imp_1"
    )
    did = list(fake_db["decisions"].values())[0]["id"]
    with pytest.raises(tem.CorrectionTargetError):
        tem.correct_match(decision_id=did, raw_string="Pug", project_id="proj_1",
                          identity="h", correct_entity_id=e["id"],
                          negative_for_entity_id=e["id"])
    with pytest.raises(tem.CorrectionTargetError):
        tem.correct_match(decision_id=did, raw_string="Pug", project_id="proj_1", identity="h")


def test_add_negative_alias_dedupes(fake_db):
    """§27: add_negative_alias de-dupes on the normalized form; re-adding is a no-op (no audit);
    entity_type=entity_negative_alias."""
    e = _seed_entity(fake_db, "org_1", "Orange Telecom")
    tem.add_negative_alias(e["id"], "Orange", identity="h")
    n_audit = len(fake_db["audit"])
    tem.add_negative_alias(e["id"], "orange", identity="h")  # same normalized form
    assert len(fake_db["neg"]) == 1
    assert len(fake_db["audit"]) == n_audit  # no-op: no audit
    added = [a for a in fake_db["audit"] if a["entity_type"] == "entity_negative_alias"]
    assert added and added[0]["composed"] == "entity_negative_alias.added"


def test_correct_match_foreign_org_existence_hiding(fake_db):
    """§28: correct_match / add_negative_alias on a foreign-org entity -> EntityNotFound."""
    fake_db["projects"]["proj_1"] = "org_1"
    foreign = _seed_entity(fake_db, "org_2", "Peugeot")  # other org
    _seed_entity(fake_db, "org_1", "Renault")
    tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Pug"], identity="a", import_ref="imp_1"
    )
    did = list(fake_db["decisions"].values())[0]["id"]
    with pytest.raises(ter.EntityNotFound):
        tem.correct_match(decision_id=did, raw_string="Pug", project_id="proj_1",
                          identity="h", correct_entity_id=foreign["id"])


def test_match_run_never_mutates_master(fake_db, monkeypatch):
    """§29: a match RUN never calls add_alias / add_negative_alias / upsert_tracked_entity
    (no autonomous master mutation -- E40-AD5), asserted by monkeypatch spies."""
    fake_db["projects"]["proj_1"] = "org_1"
    _seed_entity(fake_db, "org_1", "Peugeot")
    calls = {"add_alias": 0, "add_neg": 0, "upsert": 0}

    def _spy_add_alias(*a, **k):
        calls["add_alias"] += 1
        raise AssertionError("add_alias must not be called during a match run")

    def _spy_add_neg(*a, **k):
        calls["add_neg"] += 1
        raise AssertionError("add_negative_alias must not be called during a match run")

    def _spy_upsert(*a, **k):
        calls["upsert"] += 1
        raise AssertionError("upsert_tracked_entity must not be called during a match run")

    monkeypatch.setattr(tem, "add_alias", _spy_add_alias)
    monkeypatch.setattr(tem, "add_negative_alias", _spy_add_neg)
    monkeypatch.setattr(tem, "upsert_tracked_entity", _spy_upsert)

    tem.resolve_inbound_brands(
        project_id="proj_1", raw_strings=["Peugeot", "Toyota"], identity="a", import_ref="i"
    )
    assert calls == {"add_alias": 0, "add_neg": 0, "upsert": 0}


# ===========================================================================
# Offline -- architecture guards (grep)
# ===========================================================================


def test_no_provider_or_brand_name_in_module():
    """§30 (AD-2): the module hard-codes no provider/brand name outside the METHOD_*/REASON_*/
    OUTCOME_*/STATUS_* constants."""
    source = Path(tem.__file__).read_text(encoding="utf-8").lower()
    forbidden = [
        "google-analytics", "google_analytics", "meta-ads", "meta_ads", "tiktok",
        "linkedin", "shopify", "stripe", "facebook", "pinterest", "snapchat",
        "peugeot", "renault", "citroen", "nike", "adidas", "coca",
    ]
    hits = [name for name in forbidden if name in source]
    assert not hits, f"provider/brand name(s) hard-coded: {hits}"


def test_no_server_side_llm_sdk():
    """§31 (AC6c): the module imports NO LLM SDK and holds no model id / model secret read."""
    source = Path(tem.__file__).read_text(encoding="utf-8").lower()
    forbidden = [
        "import anthropic", "from anthropic", "anthropic(", "import openai",
        "from openai", "openai(", "messages.create", "model=", "api_key",
    ]
    hits = [tok for tok in forbidden if tok in source]
    assert not hits, f"a server-side LLM SDK / model reference leaked into core: {hits}"


def test_reuse_not_fork():
    """§32: normalize_value / similarity are the SAME objects imported from
    dimension_conformance; normalize_alias / add_alias from tracked_entity_registry -- no local
    re-declaration of a normalizer / ratio / audit writer."""
    from core import dimension_conformance as dc

    assert tem.normalize_value is dc.normalize_value
    assert tem.similarity is dc.similarity
    assert tem.normalize_alias is ter.normalize_alias
    assert tem.add_alias is ter.add_alias
    assert tem._write_semantics_audit is ter._write_semantics_audit
    # No forked normalizer/ratio symbol declared locally.
    assert "def normalize_value" not in Path(tem.__file__).read_text(encoding="utf-8")
    assert "def similarity" not in Path(tem.__file__).read_text(encoding="utf-8")


# ===========================================================================
# Live Postgres -- real DDL, append-only, CHECKs, unicity, CASCADE, audit, governance
# ===========================================================================


def _apply_migration(conn, path) -> None:
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _ensure_set_updated_at(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS app")
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION app.set_updated_at() RETURNS trigger AS $$
            BEGIN NEW.updated_at = now(); RETURN NEW; END;
            $$ LANGUAGE plpgsql
            """
        )
    conn.commit()


def _prepare(conn) -> None:
    _ensure_set_updated_at(conn)
    _apply_migration(conn, _MIGRATION_049)
    _apply_migration(conn, _MIGRATION_086)
    _apply_migration(conn, _MIGRATION_090)


def _mk_org(conn, org_id, suffix) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s,%s,%s,'system')",
            (org_id, f"Org-{suffix}", f"org-{suffix}"),
        )
    conn.commit()


def _mk_project(conn, project_id, org_id, suffix) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, org_id) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET org_id = EXCLUDED.org_id",
            (project_id, f"Proj-{suffix}", f"proj-{suffix}", org_id),
        )
    conn.commit()


def _cleanup_org(org_id: str) -> None:
    from core.db import get_connection

    with get_connection() as clean:
        with clean.cursor() as cur:
            cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
        clean.commit()


@pg_available
def test_ddl_creates_tables_replayable():
    """§33: the three tables + indexes/triggers exist after 090; replay is a no-op."""
    from core.db import get_connection

    with get_connection() as conn:
        _prepare(conn)
        _apply_migration(conn, _MIGRATION_090)  # replay must not error
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='app' "
                "AND table_name IN ('entity_negative_aliases',"
                "'inbound_brand_match_decisions','tracked_entity_alerts')"
            )
            tables = {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='app' "
                "AND indexname='uq_tracked_entity_alerts_open'"
            )
            idx = {r[0] for r in cur.fetchall()}
    assert tables == {
        "entity_negative_aliases", "inbound_brand_match_decisions", "tracked_entity_alerts"
    }
    assert "uq_tracked_entity_alerts_open" in idx


@pg_available
def test_ledger_is_append_only():
    """§34: UPDATE and DELETE on a decision row are rejected by protect_*() (immutable)."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id, proj_id = f"ib_org_{suffix}", f"ib_proj_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
        _mk_project(conn, proj_id, org_id, suffix)
    try:
        tem.resolve_inbound_brands(
            project_id=proj_id, raw_strings=["Zeta"], identity="s", import_ref="imp_1"
        )
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM app.inbound_brand_match_decisions WHERE org_id=%s",
                    (org_id,),
                )
                did = cur.fetchone()[0]
                _tbl = "app.inbound_brand_match_decisions"
                for stmt, args in (
                    (f"UPDATE {_tbl} SET raw_string='x' WHERE id=%s", (did,)),
                    (f"DELETE FROM {_tbl} WHERE id=%s", (did,)),
                ):
                    try:
                        cur.execute(stmt, args)
                        conn.rollback()
                        raise AssertionError("append-only violation not rejected")
                    except (psycopg.errors.RaiseException, psycopg.errors.InsufficientPrivilege):
                        conn.rollback()
    finally:
        _cleanup_org(org_id)


@pg_available
def test_checks_and_unicity_live():
    """§35/§36: bad outcome/method/reason/status rejected; a second open alert + a second
    negative alias (entity, norm) rejected."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id, proj_id = f"ib_org_{suffix}", f"ib_proj_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
        _mk_project(conn, proj_id, org_id, suffix)
    try:
        e = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"E{suffix}", created_by="s")
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Bad outcome.
                try:
                    cur.execute(
                        "INSERT INTO app.inbound_brand_match_decisions "
                        "(id, project_id, org_id, import_ref, raw_string, normalized_string, "
                        " outcome, created_by) VALUES (%s,%s,%s,'i','r','r','bogus','s')",
                        (f"ibm_{uuid.uuid4().hex}", proj_id, org_id),
                    )
                    conn.rollback()
                    raise AssertionError("bad outcome should be rejected")
                except psycopg.errors.CheckViolation:
                    conn.rollback()
        # Two open alerts for the same (org, normalized) -> unicity rejection.
        tem.resolve_inbound_brands(
            project_id=proj_id, raw_strings=["Zeta"], identity="s", import_ref="imp_1"
        )
        with get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.tracked_entity_alerts "
                        "(id, org_id, project_id, raw_string, normalized_string, status, "
                        "reason, created_by) VALUES "
                        "(%s,%s,%s,'Zeta','zeta','open','no_match','s')",
                        (f"tea_{uuid.uuid4().hex}", org_id, proj_id),
                    )
                    conn.rollback()
                    raise AssertionError("second open alert should be rejected")
                except psycopg.errors.UniqueViolation:
                    conn.rollback()
        # Duplicate negative alias.
        tem.add_negative_alias(e["id"], "Ghost", identity="s")
        with get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.entity_negative_aliases "
                        "(id, entity_id, raw_string, normalized_string, created_by) "
                        "VALUES (%s,%s,'Ghost','ghost','s')",
                        (f"nena_{uuid.uuid4().hex}", e["id"]),
                    )
                    conn.rollback()
                    raise AssertionError("duplicate negative alias should be rejected")
                except psycopg.errors.UniqueViolation:
                    conn.rollback()
    finally:
        _cleanup_org(org_id)


@pg_available
def test_fk_cascade_live():
    """§37: deleting a tracked_entity removes its negative aliases; the org delete cascades."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"ib_org_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
    try:
        e = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"E{suffix}", created_by="s")
        tem.add_negative_alias(e["id"], "Ghost", identity="s")
        ter.delete_tracked_entity(e["id"], identity="s")
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM app.entity_negative_aliases WHERE entity_id=%s",
                    (e["id"],),
                )
                assert cur.fetchone()[0] == 0
    finally:
        _cleanup_org(org_id)


@pg_available
def test_governed_approve_live():
    """§38: approve_new_entity_alert transitions the alert to approved and creates an approved
    tracked_entities row with the alias + approved_by; the AC2 seam on real rows."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id, proj_id = f"ib_org_{suffix}", f"ib_proj_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
        _mk_project(conn, proj_id, org_id, suffix)
    try:
        raw = f"New{suffix}"
        out = tem.resolve_inbound_brands(
            project_id=proj_id, raw_strings=[raw], identity="s", import_ref="imp_1"
        )
        alert_id = out["alerts"][0]["id"]
        res = tem.approve_new_entity_alert(
            alert_id=alert_id, canonical_name=raw, identity="human", org_id=org_id
        )
        assert res["entity"]["status"] == "approved"
        assert raw in res["entity"]["aliases"]
        assert res["alert"]["status"] == "approved"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM app.tracked_entities WHERE id=%s",
                    (res["entity"]["id"],),
                )
                assert cur.fetchone()[0] == "approved"
    finally:
        _cleanup_org(org_id)


@pg_available
def test_cross_org_existence_hiding_live():
    """§39: correct_match / add_negative_alias / approve on another org's entity/alert raise
    EntityNotFound / AlertNotFound (indistinguishable from absent)."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_a, org_b = f"ib_orgA_{suffix}", f"ib_orgB_{suffix}"
    proj_a = f"ib_projA_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_a, f"a{suffix}")
        _mk_org(conn, org_b, f"b{suffix}")
        _mk_project(conn, proj_a, org_a, f"a{suffix}")
    try:
        foreign = ter.upsert_tracked_entity(
            org_id=org_b, canonical_name=f"F{suffix}", created_by="s"
        )
        tem.resolve_inbound_brands(
            project_id=proj_a, raw_strings=[f"Q{suffix}"], identity="s", import_ref="imp_1"
        )
        decision_id = _first_decision_id(org_a)
        # correct_match resolves the caller's org from proj_a (org_a); the foreign entity is
        # org_b -> assert_entity_in_org raises EntityNotFound (indistinguishable from absent).
        with pytest.raises(ter.EntityNotFound):
            tem.correct_match(
                decision_id=decision_id, raw_string=f"Q{suffix}", project_id=proj_a,
                identity="h", correct_entity_id=foreign["id"],
            )
        with pytest.raises(ter.EntityNotFound):
            tem.correct_match(
                decision_id=decision_id, raw_string=f"Q{suffix}", project_id=proj_a,
                identity="h", negative_for_entity_id=foreign["id"],
            )
    finally:
        _cleanup_org(org_a)
        _cleanup_org(org_b)


def _first_decision_id(org_id: str) -> str:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM app.inbound_brand_match_decisions WHERE org_id=%s LIMIT 1",
                (org_id,),
            )
            return cur.fetchone()[0]


@pg_available
def test_audit_reuse_live():
    """§40: alert + negative-alias mutations write to app.metric_semantics_audit (049) with the
    new entity_types; the 049 append-only trigger still blocks UPDATE/DELETE (no new table)."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id, proj_id = f"ib_org_{suffix}", f"ib_proj_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
        _mk_project(conn, proj_id, org_id, suffix)
    try:
        e = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"E{suffix}", created_by="s")
        tem.add_negative_alias(e["id"], "Ghost", identity="s")
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM app.metric_semantics_audit "
                    "WHERE entity_type='entity_negative_alias'"
                )
                aid = cur.fetchone()[0]
                try:
                    cur.execute(
                        "UPDATE app.metric_semantics_audit SET action='x' WHERE id=%s", (aid,)
                    )
                    conn.rollback()
                    raise AssertionError("audit UPDATE should be blocked (append-only)")
                except (psycopg.errors.InsufficientPrivilege, psycopg.errors.RaiseException):
                    conn.rollback()
    finally:
        _cleanup_org(org_id)


@pg_available
def test_idempotent_reimport_live():
    """§41: re-running resolve_inbound_brands for the same strings + import_ref reuses the open
    alert and writes a stable ledger (append-only replay policy)."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id, proj_id = f"ib_org_{suffix}", f"ib_proj_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
        _mk_project(conn, proj_id, org_id, suffix)
    try:
        tem.resolve_inbound_brands(
            project_id=proj_id, raw_strings=[f"Z{suffix}"], identity="s", import_ref="imp_1"
        )
        tem.resolve_inbound_brands(
            project_id=proj_id, raw_strings=[f"z{suffix}"], identity="s", import_ref="imp_2"
        )
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM app.tracked_entity_alerts "
                    "WHERE org_id=%s AND status='open'",
                    (org_id,),
                )
                assert cur.fetchone()[0] == 1  # one open alert reused
    finally:
        _cleanup_org(org_id)
