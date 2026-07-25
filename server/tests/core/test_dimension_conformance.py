"""Tests for Story 27.4 -- conformed dimensions (value-level unification, Epic 27).

Offline (no DB): normalization + similarity + the three-stage suggestion pipeline (all
PURE), the cascade specificity reducer, persistence/curation over an in-memory FAKE
store (core.db.get_connection monkeypatched), and the AD-2 "no provider/dimension name
in the module" grep.

Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): the real DDL of migration 052,
the scope/score/status/method CHECKs, the COALESCE unicity, FK CASCADE, the audit reuse
of app.metric_semantics_audit (049), and the live cascade. Pattern calque sur
test_metric_semantics.py.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import dimension_conformance as dc  # noqa: E402

# ---------------------------------------------------------------------------
# Postgres availability check (calque sur test_metric_semantics.py)
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
_MIGRATION_049 = _REPO_ROOT / "infra" / "nango" / "migrations" / "049_metric_semantics.sql"
_MIGRATION_052 = _REPO_ROOT / "infra" / "nango" / "migrations" / "052_dimension_conformance.sql"


# ===========================================================================
# Offline -- normalization (pure, no DB)
# ===========================================================================


def test_normalize_lowercases():
    """§1: case folded ('Summer' and 'summer' collapse)."""
    assert dc.normalize_value("Summer") == dc.normalize_value("summer") == "summer"


def test_normalize_collapses_separators():
    """§2: '-', '_', '.', multiple spaces collapse to one space."""
    a = dc.normalize_value("brand_fr-summer")
    b = dc.normalize_value("brand fr  summer")
    c = dc.normalize_value("brand.fr.summer")
    assert a == b == c == "brand fr summer"


def test_normalize_strips_bracket_tags():
    """§3: bracket/paren/brace tags removed -> 'brand'."""
    assert dc.normalize_value("[FR] Brand") == "brand"
    assert dc.normalize_value("Brand (FR)") == "brand"
    assert dc.normalize_value("{promo} Brand") == "brand"


def test_normalize_strips_datestamps():
    """§4: datestamps removed -> the same form with or without the date."""
    opts = dc.NormalizeOptions()
    assert dc.normalize_value("Sale 2026-07-21", opts) == dc.normalize_value("Sale", opts)
    assert dc.normalize_value("Sale 202607", opts) == dc.normalize_value("Sale", opts)
    assert dc.normalize_value("Sale Q3 2026", opts) == dc.normalize_value("Sale", opts)


def test_normalize_does_not_merge_arbitrary_4digit_ids():
    """§4b (F-2): '<label> 1234' vs '<label> 5678' are NOT normalize-merged.

    A bare 4-digit token that is NOT a plausible year ('19xx'/'20xx') is an ID, not a
    date -- stripping it would silently fuse two distinct campaigns (the very risk this
    story fights). It must survive normalization, so the two forms stay DISTINCT."""
    a = dc.normalize_value("Campaign 1234")
    b = dc.normalize_value("Campaign 5678")
    assert a != b
    assert "1234" in a and "5678" in b
    # And the pipeline must NOT propose them as a normalized match.
    out = dc.suggest_mappings(
        {"c1": ["Campaign 1234"], "c2": ["Campaign 5678"]},
        similarity_threshold=0.999,
    )
    assert all(s.method != "normalized" for s in out)
    assert all(s.method != "exact" for s in out)


def test_normalize_does_not_merge_6_or_8_digit_ids():
    """§4c (F-2): non-date-shaped 6/8-digit ids survive (only date-shaped are stripped)."""
    # '123456' -> month '34' invalid; '12345678' -> month '34' invalid -> NOT dates.
    assert "123456" in dc.normalize_value("Ref 123456")
    assert "12345678" in dc.normalize_value("Ref 12345678")


def test_normalize_strips_date_shaped_6_and_8_digits():
    """§4d (F-2): date-shaped yyyymm / yyyymmdd DO strip; two such tokens then merge.

    'Promo 202601' and 'Promo 202512' both lose a valid yyyymm datestamp -> same form."""
    assert dc.normalize_value("Promo 202601") == dc.normalize_value("Promo 202512")
    assert dc.normalize_value("Promo 202601") == dc.normalize_value("Promo")
    assert dc.normalize_value("Batch 20260731") == dc.normalize_value("Batch")


def test_normalize_plausible_years_still_stripped():
    """§4e (F-2): plausible years (19xx/20xx) remain stripped -> 2026 and 2025 match.

    Documented behaviour: with strip_datestamps ON, 'Sale 2026' and 'Sale 2025' both drop
    the year and normalize equal (a legitimate normalized match); the honest false-friend
    lock (§13) is the strip_datestamps=False path."""
    assert dc.normalize_value("Sale 2026") == dc.normalize_value("Sale 2025")
    assert dc.normalize_value("Sale 2026") == dc.normalize_value("Sale")


def test_normalize_strips_caller_affixes():
    """§5: caller-supplied affixes removed (head or tail)."""
    opts = dc.NormalizeOptions(strip_affixes=("promo_",))
    assert dc.normalize_value("promo_summer sale", opts) == "summer sale"
    opts_suffix = dc.NormalizeOptions(strip_affixes=("_v2",))
    assert dc.normalize_value("summer sale_v2", opts_suffix) == "summer sale"


def test_normalize_folds_diacritics():
    """Accents folded so 'Ete' equivalence holds across sources."""
    assert dc.normalize_value("Soldes Ete") == dc.normalize_value("Soldes Été")


def test_normalize_idempotent():
    """§6: normalize(normalize(x)) == normalize(x)."""
    for raw in ("[FR] Summer_Sale 2026-01-01", "Brand.FR - Retargeting", "  a__b  "):
        once = dc.normalize_value(raw)
        assert dc.normalize_value(once) == once


# ===========================================================================
# Offline -- similarity & suggestion pipeline (pure)
# ===========================================================================


def test_similarity_bounds():
    """§7: identical -> 1.0 ; disjoint -> near 0."""
    assert dc.similarity("summer sale", "summer sale") == 1.0
    assert dc.similarity("aaaa", "zzzz") < 0.2


def test_stage_exact():
    """§8: identical raw values across 2 connectors -> 1 exact suggestion, score 1.0."""
    out = dc.suggest_mappings({"c1": ["Summer Sale"], "c2": ["Summer Sale"]})
    assert len(out) == 2  # one row per connector, same canonical
    assert {s.method for s in out} == {"exact"}
    assert {s.score for s in out} == {1.0}
    assert {s.canonical_value for s in out} == {"Summer Sale"}


def test_stage_normalized_same_canonical():
    """§9: 'Soldes Ete FR' vs 'soldes-ete-fr' -> normalized, score 1.0, SAME canonical."""
    out = dc.suggest_mappings(
        {"meta": ["Soldes Ete FR"], "google": ["soldes-ete-fr"]}
    )
    assert len(out) == 2
    assert {s.method for s in out} == {"normalized"}
    assert {s.score for s in out} == {1.0}
    assert len({s.canonical_value for s in out}) == 1  # one pivot


def test_normalized_pivot_deterministic():
    """The pivot is the raw value sorting first (source_value, connector) -- documented.

    Raw 'B Sale' vs 'b-sale' both normalize to 'b sale' -> a normalized match; the pivot
    is min raw by (source_value, connector) = 'B Sale' ('B'<'b' in ASCII)."""
    out = dc.suggest_mappings({"meta": ["B Sale"], "google": ["b-sale"]})
    assert {s.method for s in out} == {"normalized"}
    assert {s.canonical_value for s in out} == {"B Sale"}


def test_stage_similarity_above_threshold():
    """§10: two close labels above the threshold -> similarity, score = ratio, proposed."""
    out = dc.suggest_mappings(
        {"c1": ["Summer Sale Campaign"], "c2": ["Summer Sale Campaigns"]},
        similarity_threshold=0.8,
    )
    assert len(out) == 2
    assert {s.method for s in out} == {"similarity"}
    assert all(0.8 <= s.score < 1.0 for s in out)


def test_below_threshold_no_suggestion():
    """§11: two labels below the threshold -> NO suggestion (honest)."""
    out = dc.suggest_mappings(
        {"c1": ["Alpha Brand"], "c2": ["Omega Product"]},
        similarity_threshold=0.88,
    )
    assert out == []


def test_threshold_boundaries():
    """§12: a score just under the threshold -> nothing; at/above -> a suggestion."""
    values = {"c1": ["Summer Sale Campaign"], "c2": ["Summer Sale Campaigns"]}
    ratio = dc.similarity(
        dc.normalize_value("Summer Sale Campaign"),
        dc.normalize_value("Summer Sale Campaigns"),
    )
    assert ratio < 1.0
    # Just above the real ratio -> no suggestion.
    assert dc.suggest_mappings(values, similarity_threshold=min(1.0, ratio + 0.001)) == []
    # At the ratio -> a suggestion.
    got = dc.suggest_mappings(values, similarity_threshold=ratio)
    assert len(got) == 2


def test_false_friends_not_exact_nor_normalized():
    """§13 (KEY LOCK): 'Summer Sale 2026' vs 'Summer Sale 2025' never exact/normalized.

    With strip_datestamps ON the years vanish and the two DO normalize equal -- so this
    is a legitimate normalized match. The honest false-friend lock is with datestamps
    OFF: the forms differ, so no exact/normalized, only a possible similarity that stays
    proposed."""
    opts = dc.NormalizeOptions(strip_datestamps=False)
    out = dc.suggest_mappings(
        {"c1": ["Summer Sale 2026"], "c2": ["Summer Sale 2025"]},
        similarity_threshold=0.999,
        normalize_options=opts,
    )
    # Above 0.999 nothing survives; and none is exact/normalized.
    assert all(s.method == "similarity" for s in out)
    assert out == []  # 0.999 excludes them -> no auto anything.


def test_false_friends_retargeting_stay_proposed():
    """§13b: 'Brand FR' vs 'Brand FR Retargeting' -> at most a proposed similarity."""
    out = dc.suggest_mappings(
        {"c1": ["Brand FR"], "c2": ["Brand FR Retargeting"]},
        similarity_threshold=0.5,
    )
    # If suggested at all, it is 'similarity' and (once persisted) 'proposed' -- never
    # exact/normalized (the normalized forms differ: 'brand fr' vs 'brand fr retargeting').
    assert all(s.method == "similarity" for s in out)


def test_false_friends_persist_as_proposed_similarity(fake_db):
    """§13c (F-4): the false-friend suggestion that surfaces at 0.5 is 'similarity' AND,
    once persisted, sits at status='proposed' -- never exact/normalized, never confirmed.

    Locks the honest chain end to end: a near-but-distinct pair can be PROPOSED by the
    fuzzy stage, but it stays a proposal awaiting a human (AD-9)."""
    out = dc.suggest_mappings(
        {"c1": ["Brand FR"], "c2": ["Brand FR Retargeting"]},
        similarity_threshold=0.5,
    )
    assert out, "expected a similarity proposal at threshold 0.5"
    assert all(s.method == "similarity" for s in out)
    dc.persist_suggestions(
        out, canonical_dimension="dimA", scope_level=dc.SCOPE_ORG, org_id="org_1",
        project_id=None, identity="system",
    )
    persisted = list(fake_db["mappings"].values())
    assert persisted, "suggestions should have persisted rows"
    assert all(r["status"] == "proposed" for r in persisted)
    assert all(r["suggestion_method"] == "similarity" for r in persisted)


def test_stage_priority_exact_over_others():
    """§14: a pair classable as exact does NOT also surface as normalized/similarity."""
    out = dc.suggest_mappings(
        {"c1": ["Summer Sale"], "c2": ["Summer Sale"], "c3": ["summer-sale"]}
    )
    # c1/c2 exact; c3 shares the normalized form -> the whole trio is one canonical,
    # but the exact pair is not re-emitted as similarity. No item appears twice.
    keys = [(s.connector, s.source_value) for s in out]
    assert len(keys) == len(set(keys))  # each (connector, value) at most once
    methods_by_key = {(s.connector, s.source_value): s.method for s in out}
    assert methods_by_key[("c1", "Summer Sale")] == "exact"
    assert methods_by_key[("c2", "Summer Sale")] == "exact"


def test_pipeline_deterministic():
    """§15: same input -> identical ordered output across two calls."""
    values = {
        "c2": ["Soldes Ete", "Winter"],
        "c1": ["soldes-ete", "winter deal"],
    }
    a = dc.suggest_mappings(values, similarity_threshold=0.6)
    b = dc.suggest_mappings(values, similarity_threshold=0.6)
    assert a == b


def test_similarity_only_cross_connector():
    """Two near values in the SAME connector are NOT conformed (cross-source only)."""
    out = dc.suggest_mappings(
        {"c1": ["Summer Sale A", "Summer Sale B"]},
        similarity_threshold=0.5,
    )
    assert out == []


def test_similarity_is_greedy_pairwise_not_transitive():
    """§15b (F-3): similarity is GREEDY PAIRWISE -- no transitive closure across 3 near.

    Three connectors carrying near-but-distinct labels do NOT collapse into one canonical
    group: the first greedy pair consumes two items (both frozen), the third is left to
    pair with its own next-best free partner or emit nothing. We assert the real partial
    output shape: at most one two-item group survives, so at MOST 2 rows come out (never 3
    fused under a single canonical) -- a human reconciles the residual."""
    out = dc.suggest_mappings(
        {
            "c1": ["Summer Sale Campaign"],
            "c2": ["Summer Sale Campaigns"],
            "c3": ["Summer Sale Campaignz"],
        },
        similarity_threshold=0.8,
    )
    # Only similarity here (no exact/normalized identity across the three distinct raws).
    assert all(s.method == "similarity" for s in out)
    # Greedy: exactly one pair binds -> 2 rows share ONE canonical; never a 3-way fusion.
    assert len(out) == 2
    assert len({s.canonical_value for s in out}) == 1
    bound = {(s.connector, s.source_value) for s in out}
    assert len(bound) == 2  # the third item is left unpaired (partial, documented)


def test_no_provider_or_dimension_name_in_module():
    """§16 (AD-2): dimension_conformance.py hard-codes no connector/dimension name."""
    source = Path(dc.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    forbidden = [
        "google-analytics", "google_analytics", "meta-ads", "meta_ads",
        "tiktok", "linkedin", "shopify", "stripe", "adjust",
        "facebook", "pinterest", "amazon", "microsoft", "snapchat",
        # dimension labels must come from the caller, not the code:
        "campaign_id", "placement", "'campaign'", "breakdown_dimension",
    ]
    hits = [name for name in forbidden if name in lowered]
    assert not hits, f"provider/dimension name(s) hard-coded: {hits}"


# ===========================================================================
# Offline -- cascade reducer (pure, over in-memory rows)
# ===========================================================================


def _row(connector, source_value, canonical_value, scope, status="confirmed",
         org_id=None, project_id=None):
    return {
        "connector": connector,
        "source_value": source_value,
        "canonical_value": canonical_value,
        "scope_level": scope,
        "status": status,
        "org_id": org_id,
        "project_id": project_id,
    }


def test_reduce_confirmed_only():
    """Only confirmed rows resolve a pair (proposed/rejected ignored)."""
    rows = [
        _row("c1", "v1", "CANON", dc.SCOPE_PLATFORM, status="proposed"),
        _row("c1", "v2", "CANON2", dc.SCOPE_PLATFORM, status="confirmed"),
    ]
    resolved = dc.reduce_conformance_by_specificity(rows)
    assert resolved == {("c1", "v2"): "CANON2"}


def test_reduce_project_beats_org_beats_platform():
    """§27: PROJECT overrides ORG overrides PLATFORM for the same (connector, value)."""
    rows = [
        _row("c1", "v1", "PLAT", dc.SCOPE_PLATFORM),
        _row("c1", "v1", "ORG", dc.SCOPE_ORG, org_id="org_1"),
        _row("c1", "v1", "PROJ", dc.SCOPE_PROJECT, project_id="proj_1"),
    ]
    assert dc.reduce_conformance_by_specificity(rows) == {("c1", "v1"): "PROJ"}


def test_reduce_deterministic_repeat():
    """§30: two reductions of the same rows are identical."""
    rows = [
        _row("c1", "v1", "PLAT", dc.SCOPE_PLATFORM),
        _row("c1", "v1", "ORG", dc.SCOPE_ORG, org_id="org_1"),
    ]
    assert dc.reduce_conformance_by_specificity(rows) == dc.reduce_conformance_by_specificity(rows)


# ===========================================================================
# Offline -- scope validation (imported from 27.1, mirrors the CHECK)
# ===========================================================================


def test_scope_contracts():
    """§29: PLATFORM/ORG/PROJECT triplet contracts (via the imported validate_scope)."""
    dc.validate_scope(dc.SCOPE_PLATFORM, None, None)
    dc.validate_scope(dc.SCOPE_ORG, "org_1", None)
    dc.validate_scope(dc.SCOPE_PROJECT, None, "proj_1")
    with pytest.raises(dc.InvalidScope):
        dc.validate_scope(dc.SCOPE_PLATFORM, "org_1", None)
    with pytest.raises(dc.InvalidScope):
        dc.validate_scope(dc.SCOPE_ORG, None, None)
    with pytest.raises(dc.InvalidScope):
        dc.validate_scope(dc.SCOPE_PROJECT, "org_1", None)


# ===========================================================================
# Offline -- persistence & curation over an in-memory FAKE store (no DB)
# ===========================================================================


class _FakeTS:
    """A tiny stand-in for a Postgres TIMESTAMPTZ (has .isoformat() like a datetime)."""

    def __init__(self, label):
        self._label = label

    def isoformat(self):
        return self._label

    def __eq__(self, other):
        return isinstance(other, _FakeTS) and other._label == self._label

    def __hash__(self):
        return hash(self._label)


class _FakeCursor:
    """A minimal cursor over the fake store; supports the exact SQL shapes used."""

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
        s = " ".join(sql.split())  # collapse whitespace for matching
        self._result = None
        self.description = None

        if s.startswith("SELECT org_id FROM app.projects"):
            org = self._store["projects"].get(params[0])
            self._result = [(org,)] if org is not None else []
            return

        if s.startswith("SELECT id, source_value, canonical_value"):
            # D-3 chain/cycle check: confirmed rows of a (scope, dimension).
            # params = (scope_level, org_id, project_id, canonical_dimension, status)
            scope_level, org_id, project_id, dim, status = params
            self.description = [("id",), ("source_value",), ("canonical_value",)]
            out = []
            for r in self._store["mappings"].values():
                if (
                    r["scope_level"] == scope_level
                    and (r["org_id"] or "") == (org_id or "")
                    and (r["project_id"] or "") == (project_id or "")
                    and r["canonical_dimension"] == dim
                    and r["status"] == status
                ):
                    out.append(r)
            self._result = [
                (r["id"], r["source_value"], r["canonical_value"]) for r in out
            ]
            return

        if "FROM app.dimension_value_mappings" in s and s.startswith("SELECT"):
            self.description = [(c.strip(),) for c in dc._COLUMNS.split(",")]
            rows = self._store["mappings"]
            out = []
            if " WHERE id = " in s:
                mid = params[0]
                out = [r for r in rows.values() if r["id"] == mid]
            elif "status = 'confirmed'" in s:
                # conform_value: params = (dim, connector, source_value, org_id[, project_id]).
                # The PROJECT clause (and its param) is present only when project_id given.
                dim, connector, source_value, org_id = params[:4]
                has_project = "scope_level = 'PROJECT'" in s
                project_id = params[4] if has_project else None
                for r in rows.values():
                    if not (
                        r["canonical_dimension"] == dim
                        and r["connector"] == connector
                        and r["source_value"] == source_value
                        and r["status"] == "confirmed"
                    ):
                        continue
                    if r["scope_level"] == "PLATFORM":
                        out.append(r)
                    elif r["scope_level"] == "ORG" and r["org_id"] == org_id:
                        out.append(r)
                    elif (
                        has_project
                        and r["scope_level"] == "PROJECT"
                        and r["project_id"] == project_id
                    ):
                        out.append(r)
            elif "AND connector = %s" in s and "AND source_value = %s" in s:
                scope_level, org_id, project_id, dim, connector, source_value = params
                for r in rows.values():
                    if (
                        r["scope_level"] == scope_level
                        and (r["org_id"] or "") == (org_id or "")
                        and (r["project_id"] or "") == (project_id or "")
                        and r["canonical_dimension"] == dim
                        and r["connector"] == connector
                        and r["source_value"] == source_value
                    ):
                        out.append(r)
            elif "AND scope_level = %s" in s:
                # list_mappings_by_scope: (canonical_dimension, scope_level, org_id, project_id)
                dim, scope_level, org_id, project_id = params
                for r in rows.values():
                    if (
                        r["canonical_dimension"] == dim
                        and r["scope_level"] == scope_level
                        and (r["org_id"] or "") == (org_id or "")
                        and (r["project_id"] or "") == (project_id or "")
                    ):
                        out.append(r)
            elif "scope_level = 'PLATFORM'" in s:
                # _load_conformance_rows: canonical_dimension then optional org/project.
                dim = params[0]
                extra = params[1:]  # [org_id?, project_id?] depending on WHERE build
                org_id = extra[0] if "scope_level = 'ORG'" in s else None
                project_id = (
                    extra[-1] if "scope_level = 'PROJECT'" in s else None
                )
                for r in rows.values():
                    if r["canonical_dimension"] != dim:
                        continue
                    if r["scope_level"] == "PLATFORM":
                        out.append(r)
                    elif (
                        r["scope_level"] == "ORG"
                        and org_id is not None
                        and r["org_id"] == org_id
                    ):
                        out.append(r)
                    elif (
                        r["scope_level"] == "PROJECT"
                        and project_id is not None
                        and r["project_id"] == project_id
                    ):
                        out.append(r)
            self._result = [tuple(r[c[0]] for c in self.description) for r in out]
            return

        if s.startswith("INSERT INTO app.dimension_value_mappings"):
            self.description = [(c.strip(),) for c in dc._COLUMNS.split(",")]
            # _upsert_mapping passes 14 params (reviewed_at inlined now()) when
            # reviewed_at_now=True, else 15 (a trailing None for reviewed_at).
            reviewed_at_now = len(params) == 14
            # params order matches _upsert_mapping.
            (mid, dim, connector, source_value, canonical_value, status,
             method, score, norm, scope_level, org_id, project_id, created_by,
             reviewed_by) = params[:14]
            reviewed_at = _FakeTS("now") if reviewed_at_now else (
                (_FakeTS("now") if params[14] is not None else None)
                if len(params) > 14 else None
            )
            key = (scope_level, org_id or "", project_id or "", dim, connector, source_value)
            existing = self._store["by_key"].get(key)
            if existing is not None:
                row = self._store["mappings"][existing]
                row.update({
                    "canonical_value": canonical_value, "status": status,
                    "suggestion_method": method, "suggestion_score": score,
                    "normalized_form": norm, "reviewed_by": reviewed_by,
                    "reviewed_at": reviewed_at, "updated_at": _FakeTS("t1"),
                })
            else:
                row = {
                    "id": mid, "canonical_dimension": dim, "connector": connector,
                    "source_value": source_value, "canonical_value": canonical_value,
                    "status": status, "suggestion_method": method,
                    "suggestion_score": score, "normalized_form": norm,
                    "scope_level": scope_level, "org_id": org_id,
                    "project_id": project_id, "created_by": created_by,
                    "reviewed_by": reviewed_by, "reviewed_at": reviewed_at,
                    "created_at": _FakeTS("t0"), "updated_at": _FakeTS("t0"),
                }
                self._store["mappings"][mid] = row
                self._store["by_key"][key] = mid
            self._result = [tuple(row[c[0]] for c in self.description)]
            return

        if s.startswith("UPDATE app.dimension_value_mappings"):
            self.description = [(c.strip(),) for c in dc._COLUMNS.split(",")]
            status, canonical_value, reviewed_by, mid = params
            row = self._store["mappings"][mid]
            row.update({
                "status": status, "canonical_value": canonical_value,
                "reviewed_by": reviewed_by, "reviewed_at": _FakeTS("t1"),
                "updated_at": _FakeTS("t1"),
            })
            self._result = [tuple(row[c[0]] for c in self.description)]
            return

        if s.startswith("INSERT INTO app.metric_semantics_audit"):
            # params[2] is the composed '<entity_type>.<verb>' action (like prod); expose
            # the bare verb too for convenient assertions.
            composed = params[2]
            verb = composed.split(".", 1)[1] if "." in composed else composed
            self._store["audit"].append(
                {"action": verb, "composed": composed, "entity_type": params[3]}
            )
            return

        if s.startswith("DELETE FROM app.dimension_value_mappings"):
            mid = params[0]
            row = self._store["mappings"].pop(mid, None)
            if row is not None:
                key = (row["scope_level"], row["org_id"] or "", row["project_id"] or "",
                       row["canonical_dimension"], row["connector"], row["source_value"])
                self._store["by_key"].pop(key, None)
            return

        raise AssertionError(f"unhandled SQL in fake cursor: {s[:80]}")

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
    """Monkeypatch core.db.get_connection to a shared in-memory store."""
    from contextlib import contextmanager

    store = {"mappings": {}, "by_key": {}, "audit": [], "projects": {}}
    conn = _FakeConn(store)

    @contextmanager
    def _fake_get_connection():
        yield conn

    import core.db as db

    monkeypatch.setattr(db, "get_connection", _fake_get_connection)
    return store


def _persist_one(store, *, status="proposed", method="normalized", score=1.0,
                 connector="c1", source_value="v1", canonical="CANON",
                 scope=dc.SCOPE_ORG, org_id="org_1", dim="dimA"):
    sug = dc.Suggestion(
        connector=connector, source_value=source_value, canonical_value=canonical,
        method=method, score=score, normalized_form="norm",
    )
    return dc.persist_suggestions(
        [sug], canonical_dimension=dim, scope_level=scope, org_id=org_id,
        project_id=None, identity="system",
    )


def test_persist_writes_proposed_and_audit(fake_db):
    """§17: persist_suggestions writes status='proposed' + one 'created' audit."""
    counts = _persist_one(fake_db)
    assert counts == {"proposed": 1, "skipped": 0, "unchanged": 0}
    row = next(iter(fake_db["mappings"].values()))
    assert row["status"] == "proposed"
    assert len(fake_db["audit"]) == 1
    assert fake_db["audit"][0]["entity_type"] == "dimension_value_mapping"


def test_persist_idempotent_no_double_audit(fake_db):
    """§18: replay with identical content -> unchanged, no second audit, no duplicate."""
    _persist_one(fake_db)
    counts = _persist_one(fake_db)
    assert counts == {"proposed": 0, "skipped": 0, "unchanged": 1}
    assert len(fake_db["mappings"]) == 1
    assert len(fake_db["audit"]) == 1


def test_persist_skips_confirmed(fake_db):
    """§19: persist_suggestions never overwrites a confirmed mapping (skip counted)."""
    _persist_one(fake_db)
    mid = next(iter(fake_db["mappings"]))
    dc.confirm_mapping(mid, identity="alice")
    # A new suggestion for the same key must be skipped.
    counts = _persist_one(fake_db, canonical="OTHER")
    assert counts["skipped"] == 1
    assert fake_db["mappings"][mid]["status"] == "confirmed"
    assert fake_db["mappings"][mid]["canonical_value"] == "CANON"  # unchanged


def test_confirm_mapping_sets_reviewed_and_audits(fake_db):
    """§20: proposed -> confirmed, reviewed_by/at set, audit 'confirmed'."""
    _persist_one(fake_db)
    mid = next(iter(fake_db["mappings"]))
    audit_before = len(fake_db["audit"])
    row = dc.confirm_mapping(mid, identity="alice")
    assert row["status"] == "confirmed"
    assert row["reviewed_by"] == "alice"
    assert row["reviewed_at"] is not None
    assert len(fake_db["audit"]) == audit_before + 1
    assert fake_db["audit"][-1]["action"] == "confirmed"


def test_confirm_can_correct_canonical(fake_db):
    """confirm_mapping may adjust the pivot (canonical_value) at confirmation time."""
    _persist_one(fake_db)
    mid = next(iter(fake_db["mappings"]))
    row = dc.confirm_mapping(mid, identity="alice", canonical_value="Corrected")
    assert row["canonical_value"] == "Corrected"
    assert row["status"] == "confirmed"


def test_confirm_reconfirm_reaudits(fake_db):
    """§20b (F-5): re-confirming an already-confirmed row re-stamps + AUDITS again.

    NOT idempotent by design -- reviewed_by/reviewed_at are re-stamped (now()), so the row
    'changed' and one more append-only audit line is written. A re-confirmation is a real
    human act (who re-affirmed the mapping, when), recorded honestly."""
    _persist_one(fake_db)
    mid = next(iter(fake_db["mappings"]))
    dc.confirm_mapping(mid, identity="alice")
    audit_after_first = len(fake_db["audit"])
    assert fake_db["audit"][-1]["action"] == "confirmed"
    # Re-confirm (same canonical_value) -> a SECOND confirmed audit line.
    dc.confirm_mapping(mid, identity="bob")
    assert len(fake_db["audit"]) == audit_after_first + 1
    assert fake_db["audit"][-1]["action"] == "confirmed"
    assert fake_db["mappings"][mid]["reviewed_by"] == "bob"


def test_reject_mapping_audits(fake_db):
    """§21: proposed -> rejected, audit 'rejected'."""
    _persist_one(fake_db)
    mid = next(iter(fake_db["mappings"]))
    row = dc.reject_mapping(mid, identity="alice")
    assert row["status"] == "rejected"
    assert fake_db["audit"][-1]["action"] == "rejected"


def test_set_mapping_manual_confirmed(fake_db):
    """§22: set_mapping_manual creates a confirmed manual mapping + 'created' audit."""
    row = dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c1", source_value="v9",
        canonical_value="CANON9", scope_level=dc.SCOPE_ORG, org_id="org_1",
        project_id=None, identity="alice",
    )
    assert row["status"] == "confirmed"
    assert row["suggestion_method"] == "manual"
    assert row["reviewed_by"] == "alice"
    assert fake_db["audit"][-1]["action"] == "created"


def test_confirm_normal_ok_no_chain(fake_db):
    """D-3: a confirm that does NOT build a chain succeeds normally."""
    _persist_one(fake_db, source_value="src", canonical="TERM")
    mid = next(iter(fake_db["mappings"]))
    row = dc.confirm_mapping(mid, identity="alice")
    assert row["status"] == "confirmed"


def test_confirm_direct_chain_refused(fake_db):
    """D-3: confirming A->B while B->C already exists (B is a source elsewhere) is refused.

    Chain: a confirmed 'B'->'C' exists; confirming 'A'->'B' would make 'B' both a canonical
    (of A) and a source (of C) -> ChainedMappingError."""
    # Confirmed terminal mapping B -> C (manual so it is confirmed).
    dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c2", source_value="B",
        canonical_value="C", scope_level=dc.SCOPE_ORG, org_id="org_1",
        project_id=None, identity="alice",
    )
    # A proposed A -> B awaiting confirmation.
    _persist_one(fake_db, connector="c1", source_value="A", canonical="B")
    mid = next(m for m, r in fake_db["mappings"].items() if r["source_value"] == "A")
    with pytest.raises(dc.ChainedMappingError):
        dc.confirm_mapping(mid, identity="alice")


def test_confirm_cycle_refused(fake_db):
    """D-3: confirming A->B while X->A already exists (A is a terminal canonical) is refused.

    Our source 'A' is already a confirmed canonical_value of X->A; confirming A->B would let
    a terminal canonical become a source again (chain / cycle) -> ChainedMappingError."""
    dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c3", source_value="X",
        canonical_value="A", scope_level=dc.SCOPE_ORG, org_id="org_1",
        project_id=None, identity="alice",
    )
    _persist_one(fake_db, connector="c1", source_value="A", canonical="B")
    mid = next(m for m, r in fake_db["mappings"].items() if r["source_value"] == "A")
    with pytest.raises(dc.ChainedMappingError):
        dc.confirm_mapping(mid, identity="alice")


def test_set_manual_chain_refused(fake_db):
    """D-3: the manual path also refuses a chain (source already a confirmed canonical)."""
    dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c3", source_value="X",
        canonical_value="A", scope_level=dc.SCOPE_ORG, org_id="org_1",
        project_id=None, identity="alice",
    )
    with pytest.raises(dc.ChainedMappingError):
        dc.set_mapping_manual(
            canonical_dimension="dimA", connector="c1", source_value="A",
            canonical_value="B", scope_level=dc.SCOPE_ORG, org_id="org_1",
            project_id=None, identity="alice",
        )


def test_assert_mapping_in_org_foreign_raises(fake_db):
    """S-2: assert_mapping_in_org returns the row for its own org, and raises MappingNotFound
    (existence-hiding) for a mapping of ANOTHER org."""
    row = dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c1", source_value="v1",
        canonical_value="CANON", scope_level=dc.SCOPE_ORG, org_id="org_1",
        project_id=None, identity="alice",
    )
    mid = row["id"]
    # Same org -> the row is returned.
    got = dc.assert_mapping_in_org(mid, "org_1")
    assert got["id"] == mid
    # Foreign org -> indistinguishable from absent.
    with pytest.raises(dc.MappingNotFound):
        dc.assert_mapping_in_org(mid, "org_OTHER")
    # Unknown id -> MappingNotFound too.
    with pytest.raises(dc.MappingNotFound):
        dc.assert_mapping_in_org("dvm_does_not_exist", "org_1")


def test_conform_value_confirmed_returns_canonical(fake_db):
    """§23: conform_value on a confirmed mapping returns the canonical value."""
    dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c1", source_value="v1",
        canonical_value="CANON", scope_level=dc.SCOPE_ORG, org_id="org_1",
        project_id=None, identity="alice",
    )
    assert dc.conform_value("org_1", "dimA", "c1", "v1") == "CANON"


def test_conform_value_proposed_returns_none(fake_db):
    """§24: conform_value on a proposed mapping -> None (never resolved unvalidated)."""
    _persist_one(fake_db)  # proposed
    assert dc.conform_value("org_1", "dimA", "c1", "v1") is None


def test_conform_value_rejected_returns_none(fake_db):
    """§25: conform_value on a rejected mapping -> None."""
    _persist_one(fake_db)
    mid = next(iter(fake_db["mappings"]))
    dc.reject_mapping(mid, identity="alice")
    assert dc.conform_value("org_1", "dimA", "c1", "v1") is None


def test_conform_value_absent_returns_none(fake_db):
    """§26: conform_value on an absent value -> None (no exception)."""
    assert dc.conform_value("org_1", "dimA", "c1", "does-not-exist") is None


def test_conform_value_project_gap_without_project_id(fake_db):
    """§26b (F-1): a confirmed PROJECT override is INVISIBLE to the ORG-anchored call.

    conform_value WITHOUT project_id resolves {ORG, PLATFORM} only -- the intentional gap
    (ORG is the anchor). A confirmed PROJECT row must NOT leak into that resolution; the
    ORG value wins, and with no ORG row it falls to PLATFORM (else None)."""
    fake_db["projects"]["proj_1"] = "org_1"
    dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c1", source_value="v1",
        canonical_value="ORGVAL", scope_level=dc.SCOPE_ORG, org_id="org_1",
        project_id=None, identity="alice",
    )
    dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c1", source_value="v1",
        canonical_value="PROJVAL", scope_level=dc.SCOPE_PROJECT, org_id=None,
        project_id="proj_1", identity="alice",
    )
    # Gap locked: without project_id the PROJECT override is not seen -> ORG wins.
    assert dc.conform_value("org_1", "dimA", "c1", "v1") == "ORGVAL"


def test_conform_value_with_project_id_project_wins(fake_db):
    """§26c (F-1): WITH project_id the full cascade runs -> the PROJECT override wins."""
    fake_db["projects"]["proj_1"] = "org_1"
    dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c1", source_value="v1",
        canonical_value="ORGVAL", scope_level=dc.SCOPE_ORG, org_id="org_1",
        project_id=None, identity="alice",
    )
    dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c1", source_value="v1",
        canonical_value="PROJVAL", scope_level=dc.SCOPE_PROJECT, org_id=None,
        project_id="proj_1", identity="alice",
    )
    assert dc.conform_value("org_1", "dimA", "c1", "v1", project_id="proj_1") == "PROJVAL"


def test_conform_value_with_project_id_falls_back_to_org(fake_db):
    """§26d (F-1): project_id supplied but no PROJECT row -> ORG (then PLATFORM) still win."""
    fake_db["projects"]["proj_1"] = "org_1"
    dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c1", source_value="v1",
        canonical_value="ORGVAL", scope_level=dc.SCOPE_ORG, org_id="org_1",
        project_id=None, identity="alice",
    )
    assert dc.conform_value("org_1", "dimA", "c1", "v1", project_id="proj_1") == "ORGVAL"


def test_resolve_dimension_fail_to_platform(fake_db):
    """§28: an unknown project resolves to PLATFORM confirmed only (no crash)."""
    dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c1", source_value="v1",
        canonical_value="PLAT", scope_level=dc.SCOPE_PLATFORM, org_id=None,
        project_id=None, identity="alice",
    )
    # project 'ghost' not in projects -> org None -> PLATFORM only.
    resolved = dc.resolve_dimension_conformance("ghost", "dimA")
    assert resolved == {("c1", "v1"): "PLAT"}


def test_resolve_dimension_cascade(fake_db):
    """§27/§39: PROJECT confirmed overrides ORG confirmed for the same pair."""
    fake_db["projects"]["proj_1"] = "org_1"
    dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c1", source_value="v1",
        canonical_value="ORGVAL", scope_level=dc.SCOPE_ORG, org_id="org_1",
        project_id=None, identity="alice",
    )
    dc.set_mapping_manual(
        canonical_dimension="dimA", connector="c1", source_value="v1",
        canonical_value="PROJVAL", scope_level=dc.SCOPE_PROJECT, org_id=None,
        project_id="proj_1", identity="alice",
    )
    resolved = dc.resolve_dimension_conformance("proj_1", "dimA")
    assert resolved == {("c1", "v1"): "PROJVAL"}


# ===========================================================================
# Live Postgres -- real DDL, CHECKs, unicity, CASCADE, audit reuse, cascade
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
    """Ensure set_updated_at + 049 (audit) + 052 applied idempotently."""
    _ensure_set_updated_at(conn)
    _apply_migration(conn, _MIGRATION_049)
    _apply_migration(conn, _MIGRATION_052)


@pg_available
def test_ddl_creates_table_replayable():
    """§31: app.dimension_value_mappings + indexes exist after 052; replay is a no-op."""
    from core.db import get_connection

    with get_connection() as conn:
        _prepare(conn)
        _apply_migration(conn, _MIGRATION_052)  # replay must not error
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='app' AND table_name='dimension_value_mappings'"
            )
            assert cur.fetchone() is not None
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='app' "
                "AND tablename='dimension_value_mappings'"
            )
            idx = {r[0] for r in cur.fetchall()}
    assert "uq_dimension_value_mappings_scope_key" in idx
    assert "ix_dimension_value_mappings_resolve" in idx


@pg_available
def test_scope_check_enforced():
    """§32: Postgres rejects inconsistent scope triplets."""
    import psycopg
    from core.db import get_connection

    with get_connection() as conn:
        _prepare(conn)
        for bad in (("PLATFORM", "org_x", None), ("ORG", None, None), ("PROJECT", None, None)):
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.dimension_value_mappings "
                        "(id, canonical_dimension, connector, source_value, canonical_value,"
                        " scope_level, org_id, project_id, created_by) "
                        "VALUES (%s,'d','c','s','cv',%s,%s,%s,'system')",
                        (f"dvm_{uuid.uuid4().hex}", bad[0], bad[1], bad[2]),
                    )
                    conn.rollback()
                    raise AssertionError(f"scope {bad} should be rejected")
                except psycopg.errors.CheckViolation:
                    conn.rollback()


@pg_available
def test_score_and_status_and_method_checks():
    """§33/§34: score out of [0,1], bad status, bad method all rejected; valid accepted."""
    import psycopg
    from core.db import get_connection

    with get_connection() as conn:
        _prepare(conn)

        def _insert(score="NULL", status="'proposed'", method="NULL"):
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO app.dimension_value_mappings "
                    f"(id, canonical_dimension, connector, source_value, canonical_value,"
                    f" status, suggestion_method, suggestion_score, scope_level, created_by) "
                    f"VALUES (%s,'d','c','s','cv',{status},{method},{score},'PLATFORM','system')",
                    (f"dvm_{uuid.uuid4().hex}",),
                )

        # invalid score.
        with conn.cursor():
            try:
                _insert(score="1.5")
                conn.rollback()
                raise AssertionError("score 1.5 should be rejected")
            except psycopg.errors.CheckViolation:
                conn.rollback()
        # invalid status.
        try:
            _insert(status="'weird'")
            conn.rollback()
            raise AssertionError("bad status should be rejected")
        except psycopg.errors.CheckViolation:
            conn.rollback()
        # invalid method.
        try:
            _insert(method="'magic'")
            conn.rollback()
            raise AssertionError("bad method should be rejected")
        except psycopg.errors.CheckViolation:
            conn.rollback()
        # valid: score 0.9, method 'similarity'.
        _insert(score="0.9", method="'similarity'")
        conn.rollback()
        # valid: NULL score accepted.
        _insert()
        conn.rollback()


@pg_available
def test_coalesce_unicity_org():
    """§35: a second (ORG, org, NULL, dim, conn, val) is rejected (COALESCE unicity)."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"dc_org_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s,%s,%s,'system')",
                (org_id, f"DCOrg-{suffix}", f"dc-org-{suffix}"),
            )
        conn.commit()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.dimension_value_mappings "
                    "(id, canonical_dimension, connector, source_value, canonical_value,"
                    " scope_level, org_id, created_by) "
                    "VALUES (%s,'dimA','c1','v1','CV','ORG',%s,'system')",
                    (f"dvm_{uuid.uuid4().hex}", org_id),
                )
            conn.commit()
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.dimension_value_mappings "
                        "(id, canonical_dimension, connector, source_value, canonical_value,"
                        " scope_level, org_id, created_by) "
                        "VALUES (%s,'dimA','c1','v1','CV2','ORG',%s,'system')",
                        (f"dvm_{uuid.uuid4().hex}", org_id),
                    )
                    conn.rollback()
                    raise AssertionError("duplicate ORG mapping should be rejected")
                except psycopg.errors.UniqueViolation:
                    conn.rollback()
        finally:
            with get_connection() as clean:
                with clean.cursor() as cur:
                    cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
                clean.commit()


@pg_available
def test_live_crud_confirm_conform_and_audit():
    """§36/§37: persist -> confirm -> conform_value returns the canonical; audit written."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"dc_org_{suffix}"
    dim = f"dim_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s,%s,%s,'system')",
                (org_id, f"DCOrg-{suffix}", f"dc-org-{suffix}"),
            )
        conn.commit()
    try:
        sug = dc.Suggestion(
            connector="c1", source_value="v1", canonical_value="CANON",
            method="normalized", score=1.0, normalized_form="canon",
        )
        dc.persist_suggestions(
            [sug], canonical_dimension=dim, scope_level=dc.SCOPE_ORG,
            org_id=org_id, project_id=None, identity="system",
        )
        # proposed -> not resolvable yet.
        assert dc.conform_value(org_id, dim, "c1", "v1") is None
        rows = dc.list_mappings_by_scope(
            canonical_dimension=dim, scope_level=dc.SCOPE_ORG, org_id=org_id
        )
        assert len(rows) == 1
        dc.confirm_mapping(rows[0]["id"], identity="alice")
        assert dc.conform_value(org_id, dim, "c1", "v1") == "CANON"

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.metric_semantics_audit "
                    "WHERE entity_type='dimension_value_mapping' "
                    "AND after->>'canonical_dimension' = %s",
                    (dim,),
                )
                assert cur.fetchone()[0] >= 1
    finally:
        with get_connection() as clean:
            with clean.cursor() as cur:
                cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
            clean.commit()


@pg_available
def test_fk_cascade_org_delete_drops_org_mappings():
    """§38: deleting an org drops its ORG mappings; PLATFORM ones survive."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"dc_org_{suffix}"
    dim = f"dim_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s,%s,%s,'system')",
                (org_id, f"DCOrg-{suffix}", f"dc-org-{suffix}"),
            )
        conn.commit()

    dc.set_mapping_manual(
        canonical_dimension=dim, connector="c1", source_value="v1",
        canonical_value="PLAT", scope_level=dc.SCOPE_PLATFORM, org_id=None,
        project_id=None, identity="system",
    )
    dc.set_mapping_manual(
        canonical_dimension=dim, connector="c1", source_value="v1",
        canonical_value="ORGVAL", scope_level=dc.SCOPE_ORG, org_id=org_id,
        project_id=None, identity="system",
    )
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
            conn.commit()
        org_rows = dc.list_mappings_by_scope(
            canonical_dimension=dim, scope_level=dc.SCOPE_ORG, org_id=org_id
        )
        assert org_rows == []
        plat_rows = dc.list_mappings_by_scope(
            canonical_dimension=dim, scope_level=dc.SCOPE_PLATFORM
        )
        assert len(plat_rows) == 1
    finally:
        with get_connection() as clean:
            with clean.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.dimension_value_mappings WHERE canonical_dimension = %s",
                    (dim,),
                )
            clean.commit()
