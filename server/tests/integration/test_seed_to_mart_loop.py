"""Integration smoke test for the full local seed→load→dbt→query loop — Story 1.4, T7.

Runs:
  1. generate_seed.py (import + call)
  2. load_seed.py in DuckDB mode against tmp_path
  3. dbt run --select google_analytics (subprocess)
  4. dbt test --select google_analytics (subprocess)
  5. get_ga4_report via FastMCP in-process client

These tests are slower (~30 s) and skipped when dbt is not available in PATH.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.integration import seed_all_connectors

# Combien de temps un worker attend le seed du constructeur. Le build mesure
# 114-151 s seul (AI-106) ; la marge couvre une machine chargee sans transformer
# un constructeur mort en attente infinie.
_BUILD_WAIT_SECONDS = 900

#: CE QUI FAIT VIEILLIR UNE BASE CONSTRUITE, et il n'y avait RIEN.
#:
#: `if marker.exists(): return _hand_out_a_private_copy(...)` rendait une copie
#: du `master.duckdb` partage des que le marqueur existait, sans aucune borne.
#: Le commentaire du verrou, vingt lignes plus bas, raconte deja ce que ca coute
#: -- << Mesure du 2026-08-08 : [...] `pytest tests/integration/
#: test_seed_to_mart_loop.py -q` rendait 14 passed en 3 s sans lancer un seul
#: sous-processus dbt. Trois jours de changements dbt se sont declares verts sur
#: une base anterieure. >> -- mais SEUL LE VERROU a ete soigne. Le marqueur, qui
#: est l'autre moitie du meme piege, est reste sans borne : mesure du 2026-08-21,
#: `pytest -k published_coverage_matches_the_mart` -> 2 passed in 1.56s sur un
#: `master.duckdb` du 2026-08-19.
#:
#: UNE BORNE D'AGE AURAIT ETE LE MAUVAIS OUTIL. Elle reconstruit quand rien n'a
#: bouge, et elle accepte quand meme une base perimee dans sa fenetre. Ce qui
#: fait vieillir cette base n'est pas le temps : ce sont les FICHIERS qu'elle
#: compile. L'empreinte les lit, et le marqueur ne repond que si elle est la
#: meme -- donc un modele qui bouge force la reconstruction a la seconde pres, et
#: une journee sans changement ne reconstruit rien.
_BUILD_INPUT_GLOBS = (
    "dbt/models/**/*.sql",
    "dbt/macros/**/*.sql",
    "dbt/seeds/**/*.py",
    "dbt/dbt_project.yml",
    "server/modules/*/dbt/**/*.sql",
    "server/modules/*/seeds/*.py",
    "server/tests/integration/seed_all_connectors.py",
    # The tests and the declarations are build inputs too (2026-08-30): a
    # singular test or a schema.yml edited alone left the fingerprint unchanged,
    # so `dbt test` ran against the cached warehouse and answered a stale green.
    "dbt/tests/**/*.sql",
    "dbt/seeds/**/*.yml",
    "dbt/seeds/**/*.csv",
    "server/modules/*/dbt/**/*.yml",
    # And the MODEL declarations too (2026-08-31). `dbt/models/**/*.yml` was the
    # one half of the same hole left open: a generic test lives in a .yml beside
    # the model, and `accepted_values: {quote: false}` -- the repair for the
    # INT64/BOOL dialect class -- changes nothing else on disk. Without this
    # line the fixture loop answered green off a warehouse built before it.
    "dbt/models/**/*.yml",
)


def _build_fingerprint() -> str:
    """L'empreinte de TOUT ce que cette construction compile.

    Le chemin ET le contenu : un modele renomme change la construction autant
    qu'un modele reecrit, et un digest qui ne lirait que les contenus verrait
    les deux comme identiques.
    """
    import hashlib  # noqa: PLC0415

    root = pathlib.Path(__file__).resolve().parents[3]
    digest = hashlib.sha256()
    seen: list[pathlib.Path] = []
    for pattern in _BUILD_INPUT_GLOBS:
        seen.extend(sorted(root.glob(pattern)))
    # Un fichier peut etre attrape par deux motifs ; l'ordre et l'unicite font
    # que deux executions sur le meme arbre rendent le meme digest.
    for path in sorted(set(seen)):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    # LE COMPTE VOYAGE AVEC L'EMPREINTE. Un glob casse rendrait zero fichier et
    # un digest stable -- l'instrument mesurerait alors sa propre indulgence, ce
    # qui est exactement la forme que ce fichier repare.
    return f"{len(set(seen))}:{digest.hexdigest()}"

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parents[3]
SEEDS_DIR = REPO_ROOT / "server" / "modules" / "google-analytics" / "seeds"
META_SEEDS_DIR = REPO_ROOT / "server" / "modules" / "meta-ads" / "seeds"
DBT_DIR = REPO_ROOT / "dbt"


# ---------------------------------------------------------------------------
# Skip the whole module when dbt is not installed / not in PATH
# ---------------------------------------------------------------------------
try:  # dbt is invoked via `python -m dbt.cli.main` from the venv
    import dbt.cli.main  # noqa: F401
    _DBT_AVAILABLE = True
except ImportError:
    _DBT_AVAILABLE = False

# dbt being importable is NOT the same as this dbt project being runnable, and
# the guard used to check only the first. The fixture runs `dbt run` with no
# `--select`, so it builds EVERY staging model; a connector whose raw_* source
# is absent fails with "ERROR creating sql view model
# main_staging.stg_<connector>_daily" and the suite reports a wall of dbt output.
#
# THE PREVIOUS VERSION OF THIS GUARD MEASURED THE WRONG THING (AI-164, repaired
# 2026-08-04). It counted seed MATERIAL -- "any file under the connector's
# seeds/" -- which reached 38/38 covered, so it stopped firing. But the fixture
# INVOKED about fifteen loaders by hand, so `dbt run` was failing on ~30 absent
# raw tables while the guard reported full coverage. A guard that reports
# "covered" is worse than no guard.
#
# It now measures what the fixture actually does: a connector is covered when
# `seed_all_connectors.discover_loaders()` finds a loader for it -- the same
# derivation the fixture runs -- so the guard and the seeding can no longer
# disagree.
_MODULES_DIR = REPO_ROOT / "server" / "modules"
_WITH_STAGING = {
    path.parent.parent.parent.name for path in _MODULES_DIR.glob("*/dbt/staging/stg_*.sql")
}
_WITH_LOADER = {connector for connector, _path in seed_all_connectors.discover_loaders()}
_UNSEEDED = sorted(_WITH_STAGING - _WITH_LOADER)

#: dbt tests that fail on the all-connectors seed fixture. SHRINK-ONLY: the
#: assertion in `seeded_db` refuses a name that is not in here, and each name
#: removed is a repair that sticks. Il a tourne deux fois, et la seconde a paye :
#: 48 -> 40 le 2026-08-04, puis 41 -> 1 le 2026-08-08.
#:
#: LE CLIQUET NE POUVAIT PAS DESCENDRE TANT QU'IL NE SE LISAIT PAS. L'assertion
#: ne dit que << rien de neuf >> et jetait la liste, donc un nom qui avait CESSE
#: d'echouer y restait pour toujours -- un cliquet qui ne descend jamais est un
#: plafond. Le set mesure est desormais publie dans `built.json`
#: (`dbt_failures`, et `dbt_failures_stale` = ce qui peut etre retire), lisible
#: apres coup sans reconstruire. C'est ce champ qui a nomme les 40 a retirer.
#:
#: Le seul nom restant est explique a sa ligne.
_KNOWN_FAILING_DBT_TESTS: frozenset[str] = frozenset()
# VIDE, ET C'EST UN ETAT MESURE -- pas une suppression de garde. L'assertion en bas
# de `seeded_db` refuse toujours tout nom qui apparait : un ensemble vide veut dire
# que le PREMIER echec dbt fait rougir cette fixture, ce qui est le regime le plus
# strict que ce cliquet ait jamais eu.
#
# Sa descente, mesuree a chaque palier sur un build reel :
#     48 -> 40  le 2026-08-04  (huit assertions restatees sur le contrat de 48.3)
#     41 ->  1  le 2026-08-08  (les regles fee/tax existaient ; une colonne les
#                               empechait d'arriver -- voir seed_all_connectors)
#      1 ->  0  le 2026-08-08  (test_fx_resolution_applied decrivait un monde
#                               revolu ; reecrit comme son voisin l'avait ete)
#
# ET IL NE POUVAIT PAS DESCENDRE TANT QU'IL NE SE LISAIT PAS. L'assertion ne dit
# que << rien de neuf >> et jetait la liste, donc un nom qui avait CESSE d'echouer y
# restait pour toujours : un cliquet qui ne descend jamais est un plafond. Le set
# mesure est publie dans `built.json` -- `dbt_failures`, et `dbt_failures_stale` =
# ce qui peut etre retire. C'est ce champ qui a nomme les 41.

pytestmark = [
    # Ce fichier pilote dbt en sous-processus. Sans ce plafond il ne rate pas :
    # il ARRETE LA SESSION. `timeout = 180` + `timeout_method = "thread"`
    # (server/pyproject.toml) ne peut pas interrompre un `subprocess.run`
    # bloquant, donc pytest vide la pile et tout ce qui restait a jouer n'est
    # jamais rapporte. Mesure 2026-08-05 : la suite complete est morte ici, dans
    # la fixture `seeded_db` a l'appel `dbt seed`. Voir la garde de classe dans
    # tests/conformance/test_dbt_tests_declare_their_time.py.
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        not _DBT_AVAILABLE,
        reason="dbt not found in PATH — skipping full seed-to-mart loop integration tests",
    ),
    pytest.mark.skipif(
        bool(_UNSEEDED),
        reason=(
            f"{len(_UNSEEDED)} of {len(_WITH_STAGING)} connectors have a dbt staging model "
            f"and no seed loader that seed_all() can drive, so an unselected `dbt run` "
            f"cannot succeed: {', '.join(_UNSEEDED)}. Add a `seeds/load_<name>_seed.py` "
            f"exposing run(duckdb_path=...) for each."
        ),
    ),
]


@pytest.fixture
def anyio_backend():
    """Use asyncio as the anyio backend for async tests in this file."""
    return "asyncio"


# ---------------------------------------------------------------------------
# Importers
# ---------------------------------------------------------------------------

def _import_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gen():
    return _import_module("generate_seed", SEEDS_DIR / "generate_seed.py")


@pytest.fixture(scope="module")
def loader():
    return _import_module("load_seed", SEEDS_DIR / "load_seed.py")


@pytest.fixture(scope="module")
def seeded_db(request, tmp_path_factory, gen, loader):
    """Full pipeline: generate CSV → load DuckDB → dbt run → dbt test.

    Returns the path to the populated DuckDB file so further tests can query it.

    AI-106 -- CE MODULE ETAIT LA MOITIE VACILLANTE DE LA SUITE, ET LA CAUSE N'ETAIT
    PAS DANS DUCKDB.

    Mesure du 2026-08-05. Seul : `14 passed` trois fois de suite, en 124 / 151 /
    114 s. Sous la commande de reference `-n 8` : les huit workers tombent en
    « node down: Not properly terminated » et il reste 12 rouges sur 14 -- puis 8
    au tour suivant. Un compte d'echecs qui change a chaque execution, sur un
    arbre identique : la definition de la vacillation.

    Cette fixture est `scope="module"`, et xdist repartit les tests d'un module
    entre TOUS ses workers. Chacun exécute donc la fixture pour lui : huit
    `seed_all()`, huit `dbt seed`, huit `dbt run`, huit `dbt test` simultanes,
    chacun avec son DuckDB et son sous-processus dbt. Les workers ne rougissent
    pas, ils MEURENT -- et combien meurent depend de la memoire libre au moment
    du lancement. C'est pour cela que le nombre d'echecs bougeait.

    La reparation est de construire UNE fois par execution et de distribuer une
    COPIE, sous la meme regle qu'AI-130 : un fichier DuckDB ne se partage pas
    entre workers. Le premier a poser le verrou construit ; les autres attendent
    son marqueur, copient le resultat et repartent. En mono-process il n'y a
    qu'un worker et le chemin est le meme, sans attente.

    CE N'EST PAS UN RERUN-ON-FAILURE, et c'est deliberé : masquer un defaut
    d'isolation le transforme en vert, ce que ce depot retire partout ailleurs.
    Effet secondaire mesurable : le module coute un build au lieu de N.
    """
    tmp_dir = tmp_path_factory.mktemp("seed_loop")
    db_path = str(tmp_dir / "test.duckdb")

    # `getbasetemp()` est propre au worker ; son PARENT est commun a toute
    # l'execution. C'est le seul point de rendez-vous que xdist offre sans
    # dependance supplementaire (`filelock` n'est pas installe ici).
    shared = tmp_path_factory.getbasetemp().parent / "seed_loop_shared"
    shared.mkdir(parents=True, exist_ok=True)
    master = shared / "master.duckdb"
    marker = shared / "built.json"
    lock = shared / "build.lock"

    def _hand_out_a_private_copy(payload: dict) -> dict:
        shutil.copy2(master, db_path)
        wal = master.with_suffix(master.suffix + ".wal")
        if wal.exists():
            # Meme piege qu'AI-130 : un `.duckdb` sans son `.wal` s'ouvre en
            # silence et il lui MANQUE les dernieres ecritures -- un faux vert.
            shutil.copy2(wal, pathlib.Path(db_path).with_suffix(".duckdb.wal"))
        return {**payload, "db_path": db_path}

    fingerprint = _build_fingerprint()

    def _usable(payload: dict) -> bool:
        """Cette base a-t-elle ete construite depuis CES fichiers-la ?

        Un marqueur sans empreinte est un marqueur d'avant cette borne : il est
        refuse, donc la premiere execution apres ce changement reconstruit une
        fois, et une seule.
        """
        return payload.get("build_fingerprint") == fingerprint

    if marker.exists():
        cached = json.loads(marker.read_text(encoding="utf-8"))
        if _usable(cached):
            return _hand_out_a_private_copy(cached)
        # LE MARQUEUR PART AVANT LE VERROU. Le laisser ferait repondre le chemin
        # cache a tout worker qui arrive pendant la reconstruction, et ils
        # liraient la base d'avant pendant qu'on ecrit celle d'apres.
        marker.unlink(missing_ok=True)

    try:
        # `mkdir` est atomique sur les deux systemes de fichiers : exactement un
        # worker gagne, les autres partent attendre.
        lock.mkdir()
        # ET IL SE RELACHE. Sans cette ligne le verrou survit a l'execution qui
        # l'a pose, et le repertoire partage reste a jamais dans l'etat
        # << quelqu'un construit >>. Tant que le marqueur existe personne ne le
        # voit : le chemin cache repond. Le jour ou le marqueur disparait -- des
        # modeles qui bougent, un nettoyage, une base a reconstruire -- plus
        # aucun worker ne peut construire, ils attendent tous 900 s un
        # constructeur qui n'existe pas, et le module echoue en accusant un
        # rapport introuvable. Mesure du 2026-08-08 : le verrou de ce depot
        # datait du 2026-08-05 22:12, le marqueur du meme jour 22:14, et
        # `pytest tests/integration/test_seed_to_mart_loop.py -q` rendait
        # 14 passed en 3 s sans lancer un seul sous-processus dbt. Trois jours de
        # changements dbt se sont declares verts sur une base anterieure.
        request.addfinalizer(lambda: shutil.rmtree(lock, ignore_errors=True))
    except FileExistsError:
        deadline = time.monotonic() + _BUILD_WAIT_SECONDS
        while time.monotonic() < deadline:
            if marker.exists():
                published = json.loads(marker.read_text(encoding="utf-8"))
                # LA MEME BORNE ICI, et pour la meme raison. Un worker qui
                # attend est arrive APRES le constructeur : si le marqueur qu'il
                # trouve porte une autre empreinte, c'est celui d'avant, et le
                # prendre annulerait la reconstruction en cours pour lui seul.
                if _usable(published):
                    return _hand_out_a_private_copy(published)
            time.sleep(1.0)
        pytest.fail(
            "le worker constructeur n'a pas publie le seed en "
            f"{_BUILD_WAIT_SECONDS}s. Deux causes, et elles ne se soignent pas "
            "pareil : soit il a echoue, et son rapport dit pourquoi -- soit il "
            "n'existe pas, et le verrou est un RESTE d'une execution passee. "
            f"Regarder la date de {lock} : plus vieille que cette execution, "
            "c'est le second cas, et il se repare en le supprimant."
        )

    # 1 — Land EVERY connector's seed, not the fifteen this fixture used to
    # hand-list. `dbt run` is unselected here, so it builds every staging model,
    # and a model whose raw_* source is absent fails the whole run. Each
    # connector that landed after this block was written broke it silently; the
    # skip-guard above did not fire because it counted seed MATERIAL rather than
    # loaders actually INVOKED (AI-164). The list is now DERIVED from the tree.
    landed = seed_all_connectors.seed_all(db_path)
    assert landed, "every connector seed loader must be discovered and run"
    ga4 = landed["google-analytics/load_seed"]
    assert ga4["rows"] >= 1350, (
        f"GA4 base seed must land 90d x 3 devices x 5 countries, got {ga4['rows']}"
    )
    pull_id = ga4["pull_id"]
    assert pull_id and pull_id.startswith("pull_"), f"pull_id must start with pull_: {pull_id!r}"
    # 3 — Copy profiles.yml.example → tmp profiles dir for dbt
    profiles_dir = tmp_dir / "profiles"
    profiles_dir.mkdir()
    profiles_dst = profiles_dir / "profiles.yml"

    # Write a profiles.yml pointing at our temp DuckDB
    profiles_dst.write_text(
        f"""connector:
  target: local
  outputs:
    local:
      type: duckdb
      path: "{pathlib.PurePath(db_path).as_posix()}"
      threads: 1
""",
        encoding="utf-8",
    )

    # 2c — mirror.* relations (Story 4.4). In production mirror_sync.py lands
    # them from Postgres; here the ONE shared fixture creates them, so this
    # harness and the 24.4 equivalence test cannot drift apart again.
    seed_all_connectors.create_mirror(db_path)

    # 2d — LES CINQ SEEDERS fee/tax, DANS LEUR ORDRE DECLARE. Ils existent depuis
    # le 2026-07-27 et rien ne les appelait, ce qui laissait les relations
    # `mirror.fee_tax_*` vides et 40 `test_epic41_*` rouges -- la ligne AI-165 les
    # comptait comme un contenu a inventer alors qu'il etait ecrit.
    #
    # POURQUOI CE N'ETAIT PAS SEULEMENT UN APPEL MANQUANT, et pourquoi la note du
    # 2026-08-05 concluait de bonne foi que << seeder n'est pas la piece qui
    # manque >> : `seed_tax_fee_activation_mirror` MOURAIT, parce que
    # `create_mirror` declarait 12 colonnes sur `project_tax_fee_activation` et
    # que le seeder en fournit 13. `CREATE TABLE IF NOT EXISTS` fait gagner le
    # premier, donc le seeder ne pouvait pas corriger la forme -- seulement s'y
    # casser. La mesure prise alors etait donc prise sur une activation VIDE, et
    # chaque modele Tax lit sa porte d'activation dans cette relation depuis 48.4 :
    # zero ligne active, zero ligne partout en aval. La colonne est ajoutee dans
    # `seed_all_connectors.create_mirror` (mesuree sur la vue de production).
    #
    # Mesure du 2026-08-08, les cinq dans cet ordre sur une base neuve :
    #     61 regles, 42 projets, 39 actifs.
    # L ORDRE N EST PAS COMMUTATIF : `create_mirror` d abord, les seeders ensuite.
    # Pose l inverse le 2026-08-08 et la chaine meurt sur `Catalog Error: Table
    # with name project_preferences does not exist` -- un seeder qui la creerait
    # lui-meme deviendrait la SECONDE definition d une relation miroir, la classe
    # exacte qui a tenu 40 tests rouges.
    seed_all_connectors.seed_fee_tax(db_path)

    # 2e — LE SEEDER DU PLAN MEDIA, meme histoire que les cinq au-dessus : ecrit
    # pour la 22.4, jamais appele. `create_mirror` cree ses cinq relations VIDES,
    # donc les cinq tests dbt de pacing (`test_plan_pacing_*`,
    # `test_plan_vs_actual_ventilation_sum`) tournaient sur zero ligne et
    # passaient sans rien prouver -- un test singulier dbt passe quand il ne rend
    # aucune ligne, et zero ligne en entree en rend zero.
    #
    # Story 61.4 : c'est aussi ce qui a laisse le defaut de devise invisible. Le
    # mart etiquetait la depense reelle avec la devise du PLAN alors qu'elle sort
    # convertie dans celle du PROJET, et aucune fixture ne faisait differer les
    # deux -- il n'y avait rien a construire pour s'en apercevoir. Le seeder
    # apporte maintenant TROIS plans : celui de 22.4, un plan en USD sur un projet
    # qui restitue en EUR, et un plan dont une campagne est facturee dans une
    # devise sans taux. L'ordre compte comme au-dessus : apres `create_mirror`,
    # qui est la seule definition de forme des relations miroir.
    _import_module(
        "seed_plan_mirror", DBT_DIR / "seeds" / "mediaplan" / "seed_plan_mirror.py"
    ).run(db_path)

    # 3b — dbt seed (metric_source_priority, Story 3.7)
    env = {**os.environ, "TOOROW_DUCKDB_PATH": db_path}
    target_dir = tmp_dir / "target"
    seed_result = subprocess.run(
        [
            sys.executable, "-m", "dbt.cli.main", "seed",
            "--profiles-dir", str(profiles_dir),
            "--project-dir", str(DBT_DIR),
            "--target-path", str(target_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert seed_result.returncode == 0, (
        f"dbt seed failed:\nSTDOUT:\n{seed_result.stdout}\nSTDERR:\n{seed_result.stderr}"
    )

    # 4 — dbt run
    run_result = subprocess.run(
        [
            sys.executable, "-m", "dbt.cli.main", "run",
            "--profiles-dir", str(profiles_dir),
            "--project-dir", str(DBT_DIR),
            "--target-path", str(target_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert run_result.returncode == 0, (
        f"dbt run failed:\nSTDOUT:\n{run_result.stdout}\nSTDERR:\n{run_result.stderr}"
    )

    # 5 — dbt test
    test_result = subprocess.run(
        [
            sys.executable, "-m", "dbt.cli.main", "test",
            "--profiles-dir", str(profiles_dir),
            "--project-dir", str(DBT_DIR),
            "--target-path", str(target_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    # A RATCHET, not a green light -- same discipline as `css_lines` in
    # CLAUDE.md §5. `assert returncode == 0` was unreachable: it has never held,
    # because for as long as the fixture seeded fifteen connectors the run died
    # before dbt got to the tests, and the harness was green by ABSENCE. Now that
    # every model materialises, 808 tests actually run and 48 disagree. Those 48
    # are named below and may only SHRINK: a 49th is a regression this fixture
    # must refuse, and each one removed from the set is a repair that sticks.
    failing = {
        name
        for name in re.findall(
            r"Failure in test ([a-z0-9_]+)", test_result.stdout + test_result.stderr
        )
    }
    new_failures = sorted(failing - _KNOWN_FAILING_DBT_TESTS)
    assert not new_failures, (
        "dbt test failures not in the pinned baseline -- a model or a seed changed "
        f"a number: {new_failures}\nSTDOUT:\n{test_result.stdout}\nSTDERR:\n{test_result.stderr}"
    )

    payload = {
        # L'EMPREINTE DES ENTREES, publiee avec la base qu'elle decrit. C'est
        # elle qui fait vieillir ce marqueur, et rien d'autre.
        "build_fingerprint": fingerprint,
        "pull_id": pull_id,
        "row_count": ga4["rows"],
        "landed": json.loads(json.dumps(landed, default=str)),
        # LE CLIQUET NE PEUT RETRECIR QUE SI ON PEUT LE LIRE. L'assertion
        # ci-dessus ne dit que << rien de neuf >> ; elle jette la liste, et un nom
        # qui a CESSE d'echouer y reste alors pour toujours -- un cliquet qui ne
        # descend jamais est un plafond. Publie ici, le set se lit apres coup dans
        # `built.json` sans reconstruire, et `stale` nomme ce qu'il y a a retirer.
        "dbt_failures": sorted(failing),
        "dbt_failures_stale": sorted(_KNOWN_FAILING_DBT_TESTS - failing),
    }

    # Publier POUR LES AUTRES : la base d'abord, le marqueur ensuite, et par
    # `replace` -- un marqueur visible avant sa base ferait lire un fichier
    # incomplet, ce qui rougirait ailleurs et pour une autre raison.
    shutil.copy2(db_path, master)
    wal = pathlib.Path(db_path + ".wal")
    if wal.exists():
        shutil.copy2(wal, master.with_suffix(master.suffix + ".wal"))
    staging = marker.with_suffix(".json.partial")
    staging.write_text(json.dumps(payload), encoding="utf-8")
    staging.replace(marker)

    return {**payload, "db_path": db_path}


# ---------------------------------------------------------------------------
# L'empreinte qui fait vieillir la base -- gardee, parce qu'une borne muette est
# la meme chose qu'aucune borne.
# ---------------------------------------------------------------------------


def test_the_fingerprint_reads_a_real_corpus_and_not_an_empty_one():
    """UN GLOB CASSE REND UN DIGEST STABLE, donc un cache eternellement valide.

    C'est la forme exacte que ce fichier repare, une couche plus haut : un
    instrument qui mesure son indulgence. Le compte voyage DANS l'empreinte,
    donc il est verifiable ici sans relire les globs.
    """
    count = int(_build_fingerprint().split(":", 1)[0])
    assert count > 100, (
        f"l'empreinte ne lit que {count} fichiers : un motif de "
        "`_BUILD_INPUT_GLOBS` ne resout plus, et le cache ne peut plus perimer"
    )


def test_a_model_that_changes_changes_the_fingerprint(tmp_path, monkeypatch):
    """La propriete qui compte : un modele qui bouge force la reconstruction.

    Joue sur un ARBRE FABRIQUE plutot que sur le depot -- ecrire dans
    `dbt/models/` pour le prouver laisserait un fichier derriere en cas
    d'interruption, et cette suite construit une base a partir de ces
    fichiers-la.
    """
    import hashlib

    def _digest(root: pathlib.Path) -> str:
        h = hashlib.sha256()
        files = sorted(root.rglob("*.sql"))
        for path in files:
            h.update(path.relative_to(root).as_posix().encode("utf-8"))
            h.update(b"\0")
            h.update(path.read_bytes())
            h.update(b"\0")
        return f"{len(files)}:{h.hexdigest()}"

    models = tmp_path / "models"
    models.mkdir()
    (models / "a.sql").write_text("select 1", encoding="utf-8")
    before = _digest(tmp_path)

    # Le CONTENU change.
    (models / "a.sql").write_text("select 2", encoding="utf-8")
    assert _digest(tmp_path) != before

    # Et le NOM aussi : un digest qui ne lirait que les contenus verrait un
    # renommage comme un no-op, alors que dbt ne construit plus le meme modele.
    (models / "a.sql").write_text("select 1", encoding="utf-8")
    assert _digest(tmp_path) == before
    (models / "a.sql").rename(models / "b.sql")
    assert _digest(tmp_path) != before


# ---------------------------------------------------------------------------
# T7.1 — fact_daily_kpi has correct schema and row counts
# ---------------------------------------------------------------------------

def test_fact_daily_kpi_has_rows(seeded_db):
    """fact_daily_kpi must have rows after dbt run (AC4)."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        # dbt-duckdb materialises marts in main_marts schema
        count = con.execute("SELECT COUNT(*) FROM main_marts.fact_daily_kpi").fetchone()[0]
    finally:
        con.close()
    assert count > 0, "fact_daily_kpi must have rows after dbt run"


def test_fact_daily_kpi_schema(seeded_db):
    """fact_daily_kpi must have the canonical star-schema columns (AC4)."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        con.execute("SELECT * FROM main_marts.fact_daily_kpi LIMIT 1").fetchone()
        cols = [d[0] for d in con.description]
    finally:
        con.close()

    required = {
        "project_id", "date", "connector", "metric",
        "breakdown_dimension", "breakdown_value", "value", "pull_id", "loaded_at",
    }
    assert required.issubset(set(cols)), (
        f"Missing columns in fact_daily_kpi: {required - set(cols)}"
    )


def test_fact_daily_kpi_pull_id_propagated(seeded_db):
    """pull_id in fact_daily_kpi must match the pull_id from the load batch (AD-7)."""
    import duckdb

    db_path = seeded_db["db_path"]
    expected_pull_id = seeded_db["pull_id"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        ids = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT pull_id FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
    finally:
        con.close()

    assert expected_pull_id in ids, (
        f"Expected pull_id {expected_pull_id!r} in fact_daily_kpi, found: {ids}"
    )


def test_fact_daily_kpi_no_ratio_metrics(seeded_db):
    """fact_daily_kpi must not contain ratio metrics — additive only (AC6, AD-4)."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
    finally:
        con.close()

    forbidden = {"cvr", "ctr", "roas", "sessions_per_user"}
    found = forbidden & metrics
    assert not found, f"Ratio metrics found in fact_daily_kpi (violates AD-4): {found}"


def test_fact_daily_kpi_no_null_pull_id(seeded_db):
    """No NULL pull_id in fact_daily_kpi (AC6)."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        nulls = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi WHERE pull_id IS NULL"
        ).fetchone()[0]
    finally:
        con.close()
    assert nulls == 0, f"Found {nulls} NULL pull_id rows in fact_daily_kpi"


# ---------------------------------------------------------------------------
# Story 3.6 — Meta Ads rows flow through the shared base into fact_daily_kpi
# ---------------------------------------------------------------------------

def test_fact_daily_kpi_includes_meta_ads(seeded_db):
    """fact_daily_kpi must contain connector='meta-ads' rows after dbt run (FR2 join proof)."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        connectors = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT connector FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
        meta_metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'meta-ads'"
            ).fetchall()
        }
        meta_dims = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT breakdown_dimension FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'meta-ads'"
            ).fetchall()
        }
        # Both sources coexist in the single canonical fact table.
        both = con.execute(
            "SELECT COUNT(DISTINCT connector) FROM main_marts.fact_daily_kpi"
        ).fetchone()[0]
    finally:
        con.close()

    assert "meta-ads" in connectors, (
        f"fact_daily_kpi must include connector='meta-ads' rows, found: {connectors}"
    )
    assert "google-analytics" in connectors, "GA4 rows must remain present (no regression)"
    assert both >= 2, "google-analytics and meta-ads must coexist in the mart (gsc may join too)"
    # Meta canonical metrics (spend->cost) and dimensions land correctly.
    assert {"cost", "impressions", "clicks", "conversions"}.issubset(meta_metrics), (
        f"Meta metrics missing from mart: {meta_metrics}"
    )
    assert {"campaign_id", "adset_id", "ad_id"}.issubset(meta_dims), (
        f"Meta breakdown dimensions missing from mart: {meta_dims}"
    )


# ---------------------------------------------------------------------------
# T7.2 — get_ga4_report returns non-empty data via FastMCP in-process client
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_ga4_report_returns_real_data(seeded_db):
    """get_ga4_report returns non-empty mart data for the seeded date range (T7.2, AC5)."""
    from datetime import timedelta
    from unittest.mock import patch

    db_path = seeded_db["db_path"]

    # Import connector with env pointing to the seeded DuckDB (main_marts schema)
    with patch.dict(
        os.environ,
        {
            "TOOROW_DB_MODE": "duckdb",
            "TOOROW_DUCKDB_PATH": db_path,
        },
    ):
        connector = _import_module(
            "connector_ga4_live",
            REPO_ROOT / "server" / "modules" / "google-analytics" / "connector.py",
        )

        # Date range within the 90-day seed window. The window is derived from the
        # generator's OWN anchor, never from date.today(): the seed is pinned to
        # TOOROW_SEED_END_DATE (2026-07-15, byte-reproducible for the evals corpus),
        # so a today-relative window stops overlapping it the day the calendar
        # passes anchor+30 -- measured 2026-08-17, provenance came back None on a
        # perfectly seeded mart because today-30 was already past the last row.
        generate = _import_module("generate_seed", SEEDS_DIR / "generate_seed.py")
        anchor = generate._ANCHOR_END_DATE
        date_to = (anchor - timedelta(days=1)).isoformat()
        date_from = (anchor - timedelta(days=30)).isoformat()

        envelope = connector.get_ga4_report(
            project_id="default",
            report_profile="standard_daily",
            date_from=date_from,
            date_to=date_to,
        )

    # Shape checks
    assert "schema_version" in envelope
    assert "meta" in envelope
    assert "data" in envelope

    meta = envelope["meta"]
    assert meta["provenance"] is not None, (
        "meta.provenance must be non-None when mart has data (NFR6, AD-7)"
    )
    # Story 2.7 AC5 / NFR8: provenance is now a dict (not a scalar string).
    prov = meta["provenance"]
    assert isinstance(prov, dict), (
        f"meta.provenance must be a dict (NFR8 Story 2.7), got: {type(prov)!r}"
    )
    assert prov["source_system"] == "google-analytics"
    assert prov["source_field"] == "fact_daily_kpi"
    assert prov["pull_id"].startswith("pull_"), (
        f"provenance.pull_id {prov['pull_id']!r} must start with 'pull_'"
    )
    assert meta["freshness"] is not None, "meta.freshness must be non-None when mart has data"

    data = envelope["data"]
    metrics = data.get("metrics", {})
    assert len(metrics) > 0, "data.metrics must be non-empty for seeded date range (AC5)"
    for m in ("sessions", "active_users", "conversions"):
        assert m in metrics, f"Metric '{m}' missing from data.metrics"


# ---------------------------------------------------------------------------
# T7.3 — No code under server/ reads CSV or raw_ga4_standard_daily directly
# ---------------------------------------------------------------------------

def test_connector_does_not_read_csv_or_raw_table():
    """Connector MCP tools must not read CSV or raw tables directly (AD-12).

    Story 2.7 adds pull() which WRITES to raw_ga4_standard_daily — that
    is the landing function, not an MCP tool handler read path. The AD-12 rule
    is: MCP server reads marts only. The MCP tools (get_ga4_report, _query_mart)
    must not read raw tables. The write path in pull() is allowed.

    This test does not require a seeded DB — it inspects source code.
    """
    connector_py = REPO_ROOT / "server" / "modules" / "google-analytics" / "connector.py"
    content = connector_py.read_text(encoding="utf-8")

    # Should not open .csv files (seed files are not part of the connector)
    assert "ga4_seed.csv" not in content, (
        "connector.py must not reference ga4_seed.csv directly (AD-12)"
    )
    # AD-12: MCP read path (_MART_QUERY) must reference fact_daily_kpi (not raw tables).
    # Story 2.7 adds pull() which WRITES to raw_ga4_standard_daily — this is allowed.
    # Verify the mart query SQL only touches fact_daily_kpi.
    assert "fact_daily_kpi" in content, (
        "_MART_QUERY must reference fact_daily_kpi (AD-12 mart-only reads)"
    )
    # The _MART_QUERY block itself must not query raw tables
    mart_query_block = (
        content.split("_MART_QUERY =")[1].split("def ")[0]
        if "_MART_QUERY =" in content
        else ""
    )
    assert "raw_ga4" not in mart_query_block, (
        "_MART_QUERY SQL must NOT reference raw_ga4 (AD-12)"
    )


# ---------------------------------------------------------------------------
# Story 53.10 — le compte de couverture PUBLIE, confronte au mart REEL
# ---------------------------------------------------------------------------

def test_the_published_coverage_matches_the_mart(seeded_db):
    """Le nombre publie par la conformite doit tenir devant `main_marts.fact_daily_kpi`.

    C'EST LA GARDE QUI MANQUAIT, et son absence est ce qui a laisse passer un
    faux chiffre pendant cinq jours. `tests/conformance/
    test_fact_daily_kpi_connector_coverage.py` derive sa couverture en lisant du
    TEXTE SQL -- elle ne compte jamais une ligne. Le mart, lui, est construit
    ici. Tant que personne ne mettait les deux face a face, une lecture statique
    fausse pouvait etre imprimee, recopiee dans un document ratifie, et rester
    vraie de nulle part : `16 (42 %)` a ete publie alors que la base en rendait
    18.

    Les deux sens sont rouges, et ils ne disent pas la meme chose :
      * publie-couvert et absent du mart  -> la couverture est REVENDIQUEE a tort ;
      * present dans le mart et non publie -> la couverture est SOUS-comptee, ce
        qui est exactement le defaut de 53.10.

    Un mot sur l'identite des noms : la colonne `connector` du mart porte des
    litteraux ecrits dans les modeles, tandis que la conformite lit des noms de
    REPERTOIRE sous `server/modules/`. Les deux coincident pour TOUT l'ensemble
    publie, quelle qu'en soit la taille, et ce test est ce qui le prouve : une
    divergence d'un seul nom rougit ici plutot que de se glisser dans un compte.

    CE DOCSTRING NE DIT PLUS COMBIEN, ET C'EST DELIBERE. Il a lu << les deux
    coincident aujourd'hui pour les dix-huit >> jusqu'au 2026-08-24, alors que la
    base batie ici en rend 36 (mesure du 2026-08-24) -- un nombre fige dans le
    commentaire de la garde meme dont l'objet est d'empecher un nombre de se
    figer. Ce que cette garde tient est une EGALITE ; sa taille est ce que le
    mart et `classify()` en disent au moment ou elle tourne.
    """
    import duckdb

    from tests.conformance.test_fact_daily_kpi_connector_coverage import classify

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        observed = {
            row[0]
            for row in con.execute(
                "SELECT DISTINCT connector FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
    finally:
        con.close()

    published = classify()["in_fact"]

    # Une lecture qui echoue ne doit jamais se lire comme « zero couvert » : le
    # mart a des lignes, prouve par `test_fact_daily_kpi_has_rows` juste au-dessus,
    # donc un ensemble vide ici est un instrument casse, pas une mesure.
    assert observed, (
        "aucun connecteur distinct dans main_marts.fact_daily_kpi : le mart est "
        "vide ou la requete lit la mauvaise relation -- ce n'est pas une couverture "
        "de zero, c'est une lecture cassee"
    )

    claimed_but_absent = sorted(published - observed)
    present_but_uncounted = sorted(observed - published)
    assert not claimed_but_absent and not present_but_uncounted, (
        "le compte de couverture publie ne correspond pas au mart construit.\n"
        f"  publie comme atteignant le fait, aucune ligne dedans : {claimed_but_absent}\n"
        f"  des lignes dans le fait, absent du compte publie   : {present_but_uncounted}\n\n"
        "Soit la classification de tests/conformance/"
        "test_fact_daily_kpi_connector_coverage.py ne suit plus le graphe `ref()` "
        "jusqu'au bout, soit un modele ecrit un litteral `connector` qui ne porte "
        "pas le nom de son repertoire sous server/modules/. Les deux rendent faux "
        "le nombre publie dans docs/product-architecture/caveats-register.md."
    )


def test_fact_daily_kpi_includes_klaviyo(seeded_db):
    """Story 15.8 F-4 : fact_daily_kpi doit contenir connector='klaviyo' avec les
    metriques attributed_* apres le chargement du seed klaviyo et dbt run.
    Prouve le flux complet seed -> raw_klaviyo_daily -> staging -> mart.
    """
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        connectors = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT connector FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
        klaviyo_metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'klaviyo'"
            ).fetchall()
        }
        klaviyo_dims = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT breakdown_dimension FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'klaviyo'"
            ).fetchall()
        }
        klaviyo_count = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi WHERE connector = 'klaviyo'"
        ).fetchone()[0]
    finally:
        con.close()

    assert "klaviyo" in connectors, (
        f"fact_daily_kpi doit inclure connector='klaviyo' apres dbt run, "
        f"connecteurs presents: {connectors}"
    )
    assert klaviyo_count > 0, (
        "fact_daily_kpi doit avoir des lignes klaviyo non nulles apres dbt run"
    )
    # Metriques canoniques Klaviyo (AD-4 : noms explicites, pas 'conversions'/'revenue').
    expected_metrics = {
        "sends", "opens", "clicks", "attributed_conversions", "attributed_revenue"
    }
    assert expected_metrics.issubset(klaviyo_metrics), (
        f"Metriques Klaviyo missinges dans le mart: "
        f"{expected_metrics - klaviyo_metrics}. Presentes: {klaviyo_metrics}"
    )
    # Les deux sous-dimensions klaviyo (campaigns et flows) doivent etre presentes.
    assert {"campaign_id", "flow_id"}.issubset(klaviyo_dims), (
        f"Dimensions klaviyo missinges dans le mart: {klaviyo_dims}. "
        "Attendu: campaign_id ET flow_id (deux sous-modeles de staging)"
    )
    # Regle de non-agregation (AD-4) : 'conversions' et 'revenue' generiques interdits.
    assert "conversions" not in klaviyo_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='conversions' pour connector='klaviyo' "
        "(utiliser 'attributed_conversions' -- AD-4 non-agregation)"
    )
    assert "revenue" not in klaviyo_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='revenue' pour connector='klaviyo' "
        "(utiliser 'attributed_revenue' -- AD-4 non-agregation)"
    )


# ---------------------------------------------------------------------------
# Story 15.3 AJOUT ADDITIF : linkedin-ads dans fact_daily_kpi apres dbt build
# ---------------------------------------------------------------------------

def test_fact_daily_kpi_includes_linkedin_ads(seeded_db):
    """Story 15.3 : fact_daily_kpi doit contenir connector='linkedin-ads' avec les
    metriques canoniques (cost, impressions, clicks, conversions, leads) et les deux
    dimensions (campaign_id, campaign_group_id) apres le chargement du seed LinkedIn
    multigrain et dbt run.
    Prouve le flux complet seed -> raw_linkedin_ads_daily -> staging -> mart.
    Prouve aussi la discipline double-compte (data_level) : campaign + campaign_group
    coexistant dans raw, les deux series sont presentes SEPAREES dans le mart.
    """
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        connectors = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT connector FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
        linkedin_metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'linkedin-ads'"
            ).fetchall()
        }
        linkedin_dims = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT breakdown_dimension FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'linkedin-ads'"
            ).fetchall()
        }
        linkedin_count = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi WHERE connector = 'linkedin-ads'"
        ).fetchone()[0]
    finally:
        con.close()

    assert "linkedin-ads" in connectors, (
        f"fact_daily_kpi doit inclure connector='linkedin-ads' apres dbt run, "
        f"connecteurs presents: {connectors}"
    )
    assert linkedin_count > 0, (
        "fact_daily_kpi doit avoir des lignes linkedin-ads non nulles apres dbt run"
    )
    # Metriques canoniques LinkedIn (AD-4 : cost, impressions, clicks, conversions, leads).
    expected_metrics = {"cost", "impressions", "clicks", "conversions"}
    assert expected_metrics.issubset(linkedin_metrics), (
        f"Metriques LinkedIn missinges dans le mart: "
        f"{expected_metrics - linkedin_metrics}. Presentes: {linkedin_metrics}"
    )
    # 'leads' est optionnel (emis uniquement si non-NULL au grain campaign).
    # Pas d'assertion strict : le seed multigrain en produit, donc on verifie.
    assert "leads" in linkedin_metrics, (
        "Metrique 'leads' absente du mart linkedin-ads -- le seed multigrain doit en produire"
    )
    # Les deux dimensions LinkedIn doivent etre presentes (grain campaign ET campaign_group).
    assert "campaign_id" in linkedin_dims, (
        f"breakdown_dimension='campaign_id' absent pour linkedin-ads: {linkedin_dims}"
    )
    assert "campaign_group_id" in linkedin_dims, (
        f"breakdown_dimension='campaign_group_id' absent pour linkedin-ads: {linkedin_dims}"
        " -- prouve que stg_linkedin_ads_campaign_group_daily est bien monte"
    )
    # Discipline double-compte (lecon review-15-2 F-1) : les conversions linkedin-ads
    # restent SEPAREES du cross_source_conversions (connector distingue les series).
    # La presence de 'conversions' sous connector='linkedin-ads' est correcte et attendue.
    assert "conversions" in linkedin_metrics, (
        "fact_daily_kpi doit contenir metric='conversions' pour connector='linkedin-ads' "
        "(conversions revendiquees, separees par la cle connector -- AD-4)"
    )
    # Verifier que les totaux GA4/Meta/TikTok/Klaviyo/Shopify existants sont inchanges
    # (reconciliation "totaux existants inchanges" -- regle commune epic-15).
    assert "google-analytics" in connectors, "GA4 rows doivent rester presentes (pas de regression)"
    assert "meta-ads" in connectors, "Meta rows doivent rester presentes (pas de regression)"


# ---------------------------------------------------------------------------
# Story 15.7 AJOUT ADDITIF : stripe dans fact_daily_kpi + dedup revenue vs shopify
# ---------------------------------------------------------------------------

def test_fact_daily_kpi_includes_stripe(seeded_db):
    """Story 15.7 : fact_daily_kpi doit contenir connector='stripe' avec les metriques
    canoniques (revenue, refunds, fees, transaction_count, order_count) au grain 'day_total'
    apres le chargement du seed Stripe et dbt run. Prouve le flux complet
    seed -> raw_stripe_payments -> stg_stripe_payments_daily -> mart, sans regression sur
    les totaux existants. Prouve aussi la regle AD-4 : 'conversions' n'apparait JAMAIS pour
    stripe (un paiement est une vente, pas une conversion regie)."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        connectors = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT connector FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
        stripe_metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'stripe'"
            ).fetchall()
        }
        stripe_dims = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT breakdown_dimension FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'stripe'"
            ).fetchall()
        }
        stripe_count = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi WHERE connector = 'stripe'"
        ).fetchone()[0]
    finally:
        con.close()

    assert "stripe" in connectors, (
        f"fact_daily_kpi doit inclure connector='stripe' apres dbt run, "
        f"connecteurs presents: {connectors}"
    )
    assert stripe_count > 0, (
        "fact_daily_kpi doit avoir des lignes stripe non nulles apres dbt run"
    )
    expected_metrics = {"revenue", "refunds", "fees", "transaction_count", "order_count"}
    assert expected_metrics.issubset(stripe_metrics), (
        f"Metriques Stripe missinges dans le mart: "
        f"{expected_metrics - stripe_metrics}. Presentes: {stripe_metrics}"
    )
    # Une seule serie day-total (pas de partition par charge -- charge_id reste detail).
    assert stripe_dims == {"day_total"}, (
        f"stripe doit n'emettre que 'day_total', trouve: {stripe_dims}"
    )
    # AD-4 : pas de 'conversions' pour stripe (un paiement est une vente).
    assert "conversions" not in stripe_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='conversions' pour connector='stripe' "
        "(un paiement Stripe est une VENTE, pas une conversion regie -- AD-4)"
    )
    # Regression : les totaux existants restent presents.
    assert "shopify" in connectors, "Shopify rows doivent rester presentes (pas de regression)"
    assert "google-analytics" in connectors, "GA4 rows doivent rester presentes (pas de regression)"


def test_cross_source_revenue_dedups_stripe_and_shopify(seeded_db):
    """Story 15.7 (AD-4 dedup revenue) : sur les jours ou stripe ET shopify emettent du
    revenue (le seed correle DELIBEREMENT une majorite de charges Stripe a des commandes
    Shopify), cross_source_revenue.revenue_total choisit UNE source gagnante (shopify,
    priorite 1) et est STRICTEMENT INFERIEUR a la somme naive shopify+stripe -- preuve
    NON-tautologique que les deux revenue ne fusionnent JAMAIS dans un total croise."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        # jours de chevauchement (shopify ET stripe > 0).
        overlap = con.execute(
            """
            WITH per_source AS (
                SELECT project_id, date, connector, SUM(value) AS rev
                FROM main_marts.fact_daily_kpi
                WHERE metric = 'revenue' AND connector IN ('shopify', 'stripe')
                GROUP BY project_id, date, connector
            ),
            by_day AS (
                SELECT project_id, date,
                    MAX(CASE WHEN connector='shopify' THEN rev END) AS shop,
                    MAX(CASE WHEN connector='stripe'  THEN rev END) AS strp
                FROM per_source GROUP BY project_id, date
            )
            SELECT COUNT(*) FROM by_day
            WHERE shop IS NOT NULL AND strp IS NOT NULL AND shop > 0 AND strp > 0
            """
        ).fetchone()[0]

        # pour chaque jour de chevauchement, revenue_total < somme naive et source = shopify.
        violations = con.execute(
            """
            WITH per_source AS (
                SELECT project_id, date, connector, SUM(value) AS rev
                FROM main_marts.fact_daily_kpi
                WHERE metric = 'revenue' AND connector IN ('shopify', 'stripe')
                GROUP BY project_id, date, connector
            ),
            by_day AS (
                SELECT project_id, date,
                    MAX(CASE WHEN connector='shopify' THEN rev END) AS shop,
                    MAX(CASE WHEN connector='stripe'  THEN rev END) AS strp
                FROM per_source GROUP BY project_id, date
            ),
            overlap AS (
                SELECT project_id, date, shop + strp AS naive_sum
                FROM by_day
                WHERE shop IS NOT NULL AND strp IS NOT NULL AND shop > 0 AND strp > 0
            )
            SELECT COUNT(*)
            FROM overlap o
            JOIN main_marts.cross_source_revenue c
              ON c.project_id = o.project_id AND c.date = o.date
            WHERE c.revenue_total >= o.naive_sum - 0.001
               OR c.revenue_source <> 'shopify'
            """
        ).fetchone()[0]

        # F-1+F-2 (Story 15.7 review fix) : preuve de VRAIE correlation (jointure reelle).
        # Au moins 10 charges Stripe ont un client_reference_id qui existe dans
        # raw_shopify_orders. Si les fenetres sont desalignees (Stripe avec end_date fige,
        # Shopify avec today()), les order_ids Stripe ne matchent AUCUNE ligne Shopify et ce
        # compteur tombe a 0 -- ce qui detecte la regression de fenetre immediatement.
        real_corr = con.execute(
            """
            SELECT COUNT(*) FROM raw_stripe_payments s
            WHERE s.client_reference_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM raw_shopify_orders o
                  WHERE o.order_id = s.client_reference_id
              )
            """
        ).fetchone()[0]
    finally:
        con.close()

    assert overlap > 0, (
        "Le seed doit produire des jours ou stripe ET shopify emettent du revenue "
        "(sinon la preuve de dedup est tautologique) -- verifier la correlation du seed Stripe"
    )
    assert violations == 0, (
        f"{violations} jour(s) ou cross_source_revenue somme les deux sources ou choisit "
        "une source != shopify -- la dedup revenue Stripe x Shopify (AD-4) est violee"
    )
    assert real_corr >= 10, (
        f"Correlation Stripe x Shopify INSUFFISANTE : seulement {real_corr} charges Stripe "
        "ont un client_reference_id present dans raw_shopify_orders. "
        "Verifier que les deux seeds partagent la meme fenetre end_date (today() par defaut). "
        "Indice : charger Shopify AVANT Stripe (dependance de generation)."
    )


# ---------------------------------------------------------------------------
# Story 15.5 AJOUT ADDITIF : hubspot dans fact_daily_kpi + isolation CRM
# ---------------------------------------------------------------------------

def test_fact_daily_kpi_includes_hubspot(seeded_db):
    """Story 15.5 : fact_daily_kpi doit contenir connector='hubspot' avec les metriques
    CRM (new_contacts, deals_created, deals_closed, deal_amount) apres le chargement du
    seed HubSpot et dbt run. Prouve le flux complet :
      seed -> raw_hubspot_contacts_daily + raw_hubspot_deals_daily
           -> stg_hubspot_contacts_daily + stg_hubspot_deals_daily -> mart.
    Prouve aussi la regle AD-4 CRM : 'conversions' et 'revenue' n'apparaissent JAMAIS
    pour connector='hubspot' (noms CRM distincts = protection nominale AD-4).
    """
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        connectors = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT connector FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
        hubspot_metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'hubspot'"
            ).fetchall()
        }
        hubspot_dims = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT breakdown_dimension FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'hubspot'"
            ).fetchall()
        }
        hubspot_count = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi WHERE connector = 'hubspot'"
        ).fetchone()[0]
    finally:
        con.close()

    assert "hubspot" in connectors, (
        f"fact_daily_kpi doit inclure connector='hubspot' apres dbt run, "
        f"connecteurs presents: {connectors}"
    )
    assert hubspot_count > 0, (
        "fact_daily_kpi doit avoir des lignes hubspot non nulles apres dbt run"
    )
    # Metriques canoniques HubSpot CRM (AD-4 : noms CRM distincts des regies).
    expected_metrics = {"new_contacts", "deals_created", "deals_closed"}
    assert expected_metrics.issubset(hubspot_metrics), (
        f"Metriques HubSpot CRM missinges dans le mart: "
        f"{expected_metrics - hubspot_metrics}. Presentes: {hubspot_metrics}"
    )
    # deal_amount peut etre absent si tous les jours ont deals_closed=0 (AD-9).
    # On ne l'assert pas comme obligatoire.

    # breakdown_dimension : grain journalier CRM, jamais par entite (pas de deal_id,
    # pas de contact_id). Deux dimensions sont legitimes et deux seulement :
    #   * 'date'     -- les trois metriques de comptage (new_contacts, deals_*) ;
    #   * 'currency' -- deal_amount, qui est monetaire et porte sa devise
    #                   (fact_daily_kpi.sql, bloc hubspot story 15.5).
    # L'assertion disait `== {"date"}` et n'avait plus rien a voir avec le modele :
    # le bloc 'currency' existe depuis 15.5, et ce test n'a pas pu tourner depuis
    # que la fixture ne construisait plus (AI-164). Corrigee sur le modele, pas
    # relachee -- une troisieme dimension casse toujours.
    assert hubspot_dims <= {"date", "currency"}, (
        f"hubspot ne doit emettre que 'date' (comptages) et 'currency' (deal_amount), "
        f"trouve: {hubspot_dims}"
    )
    assert "date" in hubspot_dims, (
        f"les metriques de comptage HubSpot doivent etre au grain 'date': {hubspot_dims}"
    )
    if "deal_amount" in hubspot_metrics:
        deal_amount_dims = {
            r[0]
            for r in duckdb.connect(db_path, read_only=True).execute(
                "SELECT DISTINCT breakdown_dimension FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'hubspot' AND metric = 'deal_amount'"
            ).fetchall()
        }
        assert deal_amount_dims == {"currency"}, (
            f"deal_amount est monetaire et porte sa devise: {deal_amount_dims}"
        )

    # REGLE D'ISOLATION CRM (AD-4, CRITIQUE) : pas de collision nominale avec les regies.
    assert "conversions" not in hubspot_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='conversions' pour connector='hubspot' "
        "(new_contacts / deals != conversions regies -- AD-4 CRM isolation)"
    )
    assert "revenue" not in hubspot_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='revenue' pour connector='hubspot' "
        "(deal_amount != revenue Shopify/Stripe -- AD-4 CRM isolation)"
    )
    assert "cost" not in hubspot_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='cost' pour connector='hubspot'"
    )

    # Regression : les totaux existants (GA4, Meta, Shopify) restent presents.
    assert "google-analytics" in connectors, "GA4 rows doivent rester presentes (pas de regression)"
    assert "meta-ads" in connectors, "Meta rows doivent rester presentes (pas de regression)"
    assert "shopify" in connectors, "Shopify rows doivent rester presentes (pas de regression)"


# ---------------------------------------------------------------------------
# google-sheets: BEGIN Story 15.6 AJOUT ADDITIF : google-sheets dans fact_daily_kpi
# ---------------------------------------------------------------------------

def test_fact_daily_kpi_includes_google_sheets(seeded_db):
    """Story 15.6 : fact_daily_kpi doit contenir connector='google-sheets' avec les
    metriques objectifs (budget_declared, target_revenue, target_conversions) et
    breakdown_dimension='sheet_row_id' apres le chargement du seed Google Sheets et dbt run.
    Prouve le flux complet :
      seed -> raw_google_sheets_daily -> stg_google_sheets_daily -> mart.
    Prouve aussi la regle AD-4 Objectifs : 'conversions' et 'revenue' n'apparaissent JAMAIS
    pour connector='google-sheets' (noms objectifs distincts = protection nominale AD-4).
    """
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        connectors = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT connector FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
        gsheets_metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'google-sheets'"
            ).fetchall()
        }
        gsheets_dims = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT breakdown_dimension FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'google-sheets'"
            ).fetchall()
        }
        gsheets_count = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi WHERE connector = 'google-sheets'"
        ).fetchone()[0]
        # AI-56 seam : valeurs reelles (pas juste HTTP 200 ou presence colonne).
        gsheets_value_check = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi "
            "WHERE connector = 'google-sheets' AND value IS NOT NULL AND value > 0"
        ).fetchone()[0]
        # Grain uniqueness : chaque (project_id, date, sheet_row_id) doit etre unique par metrique.
        grain_violations = con.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT project_id, date, breakdown_value, metric, COUNT(*) AS n
                FROM main_marts.fact_daily_kpi
                WHERE connector = 'google-sheets'
                GROUP BY project_id, date, breakdown_value, metric
                HAVING n > 1
            )
            """
        ).fetchone()[0]
    finally:
        con.close()

    assert "google-sheets" in connectors, (
        f"fact_daily_kpi doit inclure connector='google-sheets' apres dbt run, "
        f"connecteurs presents: {connectors}"
    )
    assert gsheets_count > 0, (
        "fact_daily_kpi doit avoir des lignes google-sheets non nulles apres dbt run"
    )
    # Metriques canoniques Objectifs (AD-4 : noms objectifs distincts des regies).
    expected_metrics = {"budget_declared", "target_revenue", "target_conversions"}
    assert expected_metrics.issubset(gsheets_metrics), (
        f"Metriques Google Sheets missinges dans le mart: "
        f"{expected_metrics - gsheets_metrics}. Presentes: {gsheets_metrics}"
    )

    # breakdown_dimension doit etre 'sheet_row_id' (grain par canal/ligne objectif).
    assert gsheets_dims == {"sheet_row_id"}, (
        f"google-sheets doit n'emettre que breakdown_dimension='sheet_row_id', "
        f"trouve: {gsheets_dims}"
    )

    # AI-56 seam : les valeurs doivent etre reelles (pas juste des lignes vides).
    assert gsheets_value_check > 0, (
        "fact_daily_kpi google-sheets doit avoir des valeurs > 0 (AD-9 : pas de zero silencieux)"
    )

    # Grain uniqueness : (project_id, date, breakdown_value, metric) uniques.
    assert grain_violations == 0, (
        f"{grain_violations} violation(s) de grain dans fact_daily_kpi "
        "pour connector='google-sheets' "
        "(QUALIFY dans stg_google_sheets_daily doit deduplicater par pull_id DESC)"
    )

    # REGLE D'ISOLATION OBJECTIFS (AD-4, CRITIQUE) : pas de collision nominale avec les regies.
    assert "conversions" not in gsheets_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='conversions' pour connector='google-sheets' "
        "(target_conversions != conversions regies -- AD-4 isolation objectifs)"
    )
    assert "revenue" not in gsheets_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='revenue' pour connector='google-sheets' "
        "(target_revenue != revenue Shopify/Stripe -- AD-4 isolation objectifs)"
    )
    assert "cost" not in gsheets_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='cost' pour connector='google-sheets'"
    )

    # Regression : les totaux existants (GA4, HubSpot, Shopify) restent presents.
    assert "google-analytics" in connectors, "GA4 rows doivent rester presentes (pas de regression)"
    assert "hubspot" in connectors, "HubSpot rows doivent rester presentes (pas de regression)"
    assert "shopify" in connectors, "Shopify rows doivent rester presentes (pas de regression)"

# ---------------------------------------------------------------------------
# google-sheets: END Story 15.6 block.
# ---------------------------------------------------------------------------
