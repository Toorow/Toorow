"""T2: Meta-test for reference SQL execution against DuckDB seeds (Story 14.1).

Executes every reference_sql in corpus.yaml against local DuckDB and asserts that:
1. Every query executes without error.
2. The SHA-256 of the JSON result matches fixture_sha256.
3. Designed empty-state queries (expected_empty=true) return 0 rows.

Skips gracefully if TOOROW_DUCKDB_PATH is absent or file does not exist.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import duckdb
import pytest
import yaml

DEFAULT_DUCKDB = (
    Path(__file__).parents[2] / "modules" / "google-analytics" / "seeds" / "local.duckdb"
)


def _seed_source() -> Path | None:
    env_path = os.environ.get("TOOROW_DUCKDB_PATH")
    if env_path:
        p = Path(env_path)
        return p if p.is_file() else None
    return DEFAULT_DUCKDB if DEFAULT_DUCKDB.is_file() else None


def test_reference_sql_green_on_seeds(duckdb_seed_copy):
    # AI-106 : meme defaut d'isolation que test_corpus_determinism -- ce test
    # ouvrait le seed PARTAGE en lecture-ecriture et y creait des vues `marts.*`.
    # Sous `-n 8` le verrou exclusif de DuckDB faisait rougir un test correct.
    # `duckdb_seed_copy` rend une copie privee au worker : memes octets, donc le
    # sha-exact de ce test reste un vrai verdict.
    source = _seed_source()
    if not source:
        pytest.skip(
            "TOOROW_DUCKDB_PATH not set or file not found — reference SQL green gate skipped"
        )
    db_path = duckdb_seed_copy(source)

    evals_dir = Path(__file__).parent
    corpus_path = evals_dir / "corpus.yaml"
    data = yaml.safe_load(corpus_path.read_text(encoding="utf-8"))

    conn = duckdb.connect(str(db_path))
    # Determinism: pin single-threaded, insertion-ordered aggregation so SUM() over floats
    # reduces in the SAME order as the fixture builder (build_eval_corpus_and_fixtures.py).
    # Parallel reduction reorders adds -> IEEE-754 trailing-bit noise on fractional metrics
    # (e.g. cost 27056.899999999998 vs ...987), which would make this sha-exact gate flaky.
    conn.execute("SET threads TO 1")
    conn.execute("SET preserve_insertion_order TO true")
    conn.execute("CREATE SCHEMA IF NOT EXISTS marts")
    marts_tables = conn.execute("SHOW TABLES FROM main_marts").fetchall()
    for (tbl,) in marts_tables:
        conn.execute(f"CREATE VIEW IF NOT EXISTS marts.{tbl} AS SELECT * FROM main_marts.{tbl}")

    # AI-213 -- COMBIEN, ET PAS LAQUELLE.
    #
    # Ce test s'arretait sur la PREMIERE empreinte fausse. Une divergence de
    # corpus entier et une regression d'une seule requete rendaient donc le meme
    # message -- « Question 'X' SHA-256 mismatch » -- et ce message accuse la
    # requete nommee, ce qu'un lecteur comprend comme « le SQL de reference a
    # change ». Les 57 autres ne s'executaient pas.
    #
    # Ce sont pourtant deux diagnostics opposes : 1 sur 58, c'est cette requete ;
    # 58 sur 58, c'est l'ENTREE qui n'est plus celle des fixtures, et aucune
    # requete n'est en cause.
    #
    # RE-EPINGLE LE 2026-08-17 (AI-213, etape 2). Les fixtures ne sont plus
    # epinglees sur un mart perdu : elles le sont sur un entrepot que
    # `server/modules/google-analytics/seeds/run_local_loop.py` rebatit de zero,
    # sur le corpus ancre (TOOROW_SEED_END_DATE, defaut 2026-07-19).
    #
    # CE QUE LE REBUILD A TROUVE, et qui explique les 40 divergences d'alors :
    # l'ancien `local.duckdb` etait un DEPOT D'ALLUVIONS, pas une construction.
    # `raw_ga4_standard_daily` y portait 51 300 lignes sur 2026-04-11 -> 2026-07-19,
    # soit 38 chargements empiles de fenetres differentes ; une construction propre
    # en rend 1 350 (90 jours x 3 appareils x 5 pays) sur 2026-04-16 -> 2026-07-14.
    # Aucune machine ne pouvait le reproduire : il fallait avoir joue les memes
    # chargements dans le meme ordre depuis des mois.
    #
    # Pour rebatir et re-epingler :
    #     uv run python server/modules/google-analytics/seeds/run_local_loop.py
    #     uv run python server/tests/evals/build_eval_corpus_and_fixtures.py
    # Joue deux fois, il rend les 58 memes empreintes (mesure du 2026-08-17).
    mismatches: list[str] = []
    checked = 0

    for q in data.get("questions", []):
        q_id = q["id"]
        for idx, q_entry in enumerate(q.get("reference_queries", [])):
            sql = q_entry["reference_sql"]
            expected_sha = q_entry["fixture_sha256"]
            expected_empty = q_entry.get("expected_empty", False)
            fixture_rel = q_entry["expected_result_fixture"]

            rel = conn.execute(sql)
            cols = [desc[0] for desc in rel.description] if rel.description else []
            rows = rel.fetchall()

            row_dicts = []
            for r in rows:
                rd = {}
                for col, val in zip(cols, r):
                    if hasattr(val, "isoformat"):
                        val = val.isoformat()
                    rd[col] = val
                row_dicts.append(rd)

            # FIX 2026-07-20 (scoring-honesty): removed the COUNT=0->[] coercion heuristic.
            # A single row with all-zero/null values is a REAL result (e.g. COUNT(*) AS x = 0
            # means "no gaps found" — that is information, not an empty set). The empty-set
            # contract is expressed exclusively via expected_empty=true in corpus.yaml, which
            # covers only queries that return EXACTLY 0 rows by design (GROUP BY over absent
            # data). COUNT/SUM aggregates without GROUP BY always return one row (null/zero)
            # and must NOT be marked expected_empty; their fixture captures the real result.
            #
            # FIX 2026-07-21 (Fix 1 — strict expected_empty): remove is_null_aggregate soft
            # catch-all. expected_empty now means strictly 0 rows — no exceptions. A scored
            # (non-empty-expected) question can NEVER be satisfied by 0 rows.
            if expected_empty:
                assert len(row_dicts) == 0, (
                    f"Question '{q_id}' query [{idx}] marked expected_empty=true but returned "
                    f"{len(row_dicts)} row(s): {row_dicts}. "
                    "expected_empty is strictly 0 rows. Aggregate queries (COUNT/SUM without "
                    "GROUP BY) always return one row — drop expected_empty and pin the "
                    "real fixture."
                )

            fixture_bytes = json.dumps(
                row_dicts, sort_keys=True, indent=2, ensure_ascii=False
            ).encode("utf-8")
            actual_sha = hashlib.sha256(fixture_bytes).hexdigest()

            checked += 1
            if actual_sha != expected_sha:
                mismatches.append(
                    f"  {q_id} [{idx}] -- {fixture_rel}\n"
                    f"      expected {expected_sha[:16]}...  got {actual_sha[:16]}..."
                )

    if mismatches:
        # The COUNT carries the diagnosis, so it is the first thing said.
        # ONE mismatch is about its query. MANY, spread across unrelated
        # questions, is about the input -- and the ones that still hold are the
        # queries blind to whatever changed, not evidence that the input is fine.
        # The threshold is deliberately not a diagnosis: the message states both
        # readings and gives the number that separates them.
        few = len(mismatches) <= 2
        verdict = (
            f"{checked - len(mismatches)} of {checked} still hold, so this is not "
            "a wholesale replacement -- it is an input change that only some "
            "queries observe.\n"
            "THE WAREHOUSE UNDER THIS TEST IS DERIVABLE since 2026-08-17 "
            "(AI-213), so the first question is no longer `which query broke` but "
            "`is this warehouse the one the repo builds`. Rebuild it and re-run "
            "before reading anything else into these numbers:\n"
            "    uv run python server/modules/google-analytics/seeds/run_local_loop.py\n"
            "It lands every connector seed on the corpus anchor "
            "(TOOROW_SEED_END_DATE, default 2026-07-19; the GA4 family keeps its "
            "AI-66 anchor 2026-07-15), creates the mirror relations, runs the "
            "fee/tax and media-plan seeders, then `dbt seed/run/test`. Two "
            "consecutive rebuilds produce the same 58 fixtures byte for byte, so "
            "a mass divergence AFTER a rebuild means the corpus itself moved -- a "
            "generator, a loader, a staging model or a mart -- and the number to "
            "hunt is the one that changed there, not a fixture.\n"
            "If the rebuild clears it, the warehouse had DRIFTED: seed loaders "
            "append, so a `local.duckdb` that is months old is an accumulation of "
            "past loads rather than a build. That is exactly what the 2026-08-17 "
            "re-pin found -- 51 300 rows in `raw_ga4_standard_daily` where a "
            "clean build lands 1 350.\n"
            "Only then re-pin, and only with the versioned builder "
            "(`server/tests/evals/build_eval_corpus_and_fixtures.py`). NEVER edit "
            "a `fixture_sha256` by hand: that is a green by adjustment, and it "
            "buys back the exact blindness this test spent three measurements "
            "removing. Tracked as AI-213."
            if not few
            else "Only one or two diverge, so these queries are the subject: an "
            "input change moves many unrelated fixtures at once, not two."
        )
        raise AssertionError(
            f"{len(mismatches)} of {checked} reference fixtures diverge.\n"
            f"{verdict}\n\n" + "\n".join(mismatches[:10])
            + (f"\n  ... and {len(mismatches) - 10} more" if len(mismatches) > 10 else "")
        )
