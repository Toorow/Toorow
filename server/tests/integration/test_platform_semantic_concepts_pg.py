"""Le referentiel de plateforme ecrit vraiment, une seule fois, et n ecrase jamais.

Quatre proprietes ne se prouvent nulle part ailleurs :

  * le mint ne produit que la forme PUBLIABLE. Ce fichier pinglait l inverse
    jusqu au 2026-08-31 : il exigeait que `roas`, `ctr` et `cpa` existent en tete
    apres le provisionnement, c est-a-dire trois versions `published` a
    `project_id = NULL` dont les operandes ne sont portes que par NOM. C est
    exactement la forme que l amendement du 2026-08-25 de `governance.md` liste
    en `Incomplete if` -- << une metrique calculee du catalogue livre est mintee
    a la portee PLATEFORME : ses operandes ne peuvent pas y etre epingles, donc
    la version serait impubliable par construction >> -- et un test vert la
    tenait en place. Mesure du meme jour, sur une sonde annulee : le mint les
    ecrivait bel et bien, ce qui refute aussi la phrase de `known-debt.json` qui
    disait que cette voie << n a jamais ete disponible de toute facon >> ;
  * relancer est sans effet -- un referentiel partage par toute l instance ne
    tolere pas un doublon ;
  * la version 1 est IMMUABLE. Corriger un semis n est donc pas une edition :
    c est une version 2 et un pointeur qui bouge, et l histoire garde ce que la
    migration avait ecrit ;
  * ce qui est retenu est NOMME, avec le geste qui le rend disponible. Un
    catalogue silencieux sur ce qu il ne mint pas se lit comme un catalogue
    complet.

`live_postgres` fait le rollback au teardown.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from core import platform_semantic_concepts as referential  # noqa: E402


def _heads(conn) -> dict[str, dict]:
    return referential.load_platform_concepts(conn)


def test_the_delivered_catalogue_lands_in_the_shape_a_platform_scope_can_publish(
    live_postgres,
):
    """Ce qui est minte est publiable ; ce qui ne l est pas est retenu et nomme."""
    report = referential.provision(live_postgres, actor="tester@example.com")
    heads = _heads(live_postgres)

    assert report["refused"] == []
    #  L ETAT FINAL, jamais le compte de ce run : sur une base ou la migration a
    #  deja seme, `minted` est plus petit que le catalogue.
    for name, head in heads.items():
        assert referential.unresolved_operand_names(head["expression"]) == [], (
            f"{name}: une version de PLATEFORME porte un operande par nom, donc "
            "impubliable par construction"
        )

    #  Les trois metriques CALCULEES du catalogue livre ne sont pas du vocabulaire
    #  de plateforme : ce sont des presets qu un projet adopte, et l adoption
    #  epingle chaque operande a une version exacte.
    assert {"roas", "ctr", "cpa"}.isdisjoint(set(report["minted"]))
    withheld = "; ".join(report["withheld"])
    for name in ("roas", "ctr", "cpa"):
        assert f"{name}: adopt it in a Project" in withheld, withheld
    #  Le refus nomme le GESTE, pas la cause technique.
    assert "unpublishable by construction" in withheld


def test_the_mint_door_refuses_the_unpublishable_shape_whoever_calls_it(live_postgres):
    """La garde est a la PORTE, pas seulement chez l appelant.

    Sans elle, un script, un test ou un futur appelant pourrait ecrire a la main
    la version que `provision` retient -- et une version publiee est immuable,
    donc la faute ne se rattraperait pas.
    """
    calculated = referential.PlatformConcept(
        name=_unique_name("calculated_metric"),
        value_type="ratio",
        expression={
            "op": "ratio",
            "zero_denominator": "null",
            "numerator": {"op": "concept_name", "name": "revenue"},
            "denominator": {"op": "concept_name", "name": "cost"},
        },
        additivity_class="non_additive",
        aggregation=None,
    )
    assert calculated.unresolved_operands == ["cost", "revenue"]
    assert calculated.publishable_at_platform_scope is False

    with pytest.raises(referential.PlatformConceptError) as refusal:
        referential.declare_platform_concept(
            live_postgres, calculated, actor="tester@example.com"
        )
    assert "adopt it in a Project" in str(refusal.value)

    with pytest.raises(referential.PlatformConceptError):
        referential.repair_seeded_concept(
            live_postgres, calculated, actor="tester@example.com"
        )

    #  ET RIEN N EST ECRIT : un refus qui laisse une ligne derriere lui est pire
    #  qu une absence de refus.
    assert calculated.name not in _heads(live_postgres)


def test_the_projection_still_expresses_the_calculated_metrics_it_withholds(
    live_postgres,
):
    """Retenir le MINT n est pas retirer la metrique du catalogue.

    `semantic_metric_presets` lit la MEME projection pour offrir l adoption, et
    `check_metric_formula_parity` compare la MEME forme d arbre. Une projection
    amputee des ratios rendrait la garde a trois sources aveugle et l offre vide.
    """
    projected, refused = referential.project_delivered_catalogue(
        referential.load_dictionary_types(live_postgres)
    )
    assert refused == []
    by_name = {concept.name: concept for concept in projected}
    for name in ("roas", "ctr", "cpa"):
        assert by_name[name].expression["op"] == "ratio"
        assert by_name[name].additivity_class == "non_additive"
        assert by_name[name].publishable_at_platform_scope is False


def test_running_it_twice_declares_nothing_the_second_time(live_postgres):
    referential.provision(live_postgres, actor="tester@example.com")
    before = set(_heads(live_postgres))

    second = referential.provision(live_postgres, actor="tester@example.com")

    assert second["minted"] == []
    assert set(_heads(live_postgres)) == before




def test_repair_only_corrects_the_seed_without_enlarging_the_vocabulary(live_postgres):
    """Corriger un semis et agrandir le vocabulaire sont DEUX actes, et la porte le prouve.

    Une session qui doit remettre `cost` sur `money` -- ce que le catalogue livre
    declare deja -- ne doit pas etre forcee a minter au passage les concepts
    manquants, que TOUT projet de l instance lira ensuite. `mint=False` est cette
    separation, et sans ce test il n y aurait rien pour empecher un refactor de
    remettre les deux actes ensemble.
    """
    before = set(_heads(live_postgres))
    planned = referential.provision(
        live_postgres, actor="tester@example.com", dry_run=True, repair_seeded=True
    )

    report = referential.provision(
        live_postgres, actor="tester@example.com", repair_seeded=True, mint=False
    )

    assert report["minted"] == []
    #  Aucun NOM nouveau : le vocabulaire partage n a pas bouge d une ligne.
    assert set(_heads(live_postgres)) == before
    #  Et la reparation a bien eu lieu -- exactement celle que la porte annoncait.
    assert report["repaired"] == planned["would_repair"]
    after = referential.provision(
        live_postgres, actor="tester@example.com", dry_run=True, repair_seeded=True
    )
    assert after["would_repair"] == []
    #  Ce que `mint=False` a laisse a faire reste annonce, jamais tu.
    assert after["would_mint"] == planned["would_mint"]


def _seed_like_migration_142(conn, name: str, *, author: str) -> tuple[str, str]:
    """Un concept de plateforme tel que la migration 142 l a seme : version 1,
    `additive`, `SUM` -- la contradiction mesuree le 2026-08-17.

    Construit ICI plutot que lu dans la base : un test qui depend de ce qu un
    cluster porte deja se saute au lieu de prouver, et c est exactement le defaut
    que ces suites viennent de corriger ailleurs.
    """
    from ulid import ULID

    concept_id, version_id = f"sc_{ULID()}", f"scv_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_concepts "
            "(id, project_id, kind, name, lifecycle_status, current_version_id, created_by) "
            "VALUES (%s, NULL, 'metric', %s, 'published', %s, %s)",
            (concept_id, name, version_id, author),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_concept_versions
                (id, concept_id, project_id, version_number, status, kind, name, label,
                 value_type, expression, aggregation, additivity_class, content_hash, created_by)
            VALUES (%s, %s, NULL, 1, 'published', 'metric', %s, %s, 'decimal',
                    %s::jsonb, '{"function": "sum"}'::jsonb, 'additive', %s, %s)
            """,
            (
                version_id,
                concept_id,
                name,
                name.capitalize(),
                '{"op": "source_measure", "concept": "%s"}' % name,
                "0" * 64,
                author,
            ),
        )
    return concept_id, version_id


def _contradicted(name: str) -> referential.PlatformConcept:
    """Ce que le catalogue livre declare, face au semis `additive` de la migration.

    Le NOM est synthetique et unique : `uq_semantic_concepts_name_platform` refuse
    un doublon, et un test qui reutiliserait `average_position` dependrait de ce
    que le cluster porte deja -- exactement ce que ces suites viennent de corriger.
    """
    return referential.PlatformConcept(
        name=name,
        value_type="decimal",
        expression={"op": "source_measure", "concept": name},
        additivity_class="non_additive",
        aggregation=None,
    )


def _unique_name(prefix: str) -> str:
    from ulid import ULID

    return f"{prefix}_{str(ULID())[:10].lower()}"


def test_a_seeded_contradiction_is_repaired_by_a_NEW_version_never_an_edit(live_postgres):
    """La version 1 reste, et dit ce que la migration avait ecrit.

    Le trigger `trg_semantic_concept_versions_immutable` refuse tout UPDATE, et
    c est bien : l histoire d une definition gouvernee ne se reecrit pas. Corriger
    est donc une version 2 et un pointeur qui bouge.
    """
    name = _unique_name("seeded_metric")
    _concept_id, first_version = _seed_like_migration_142(live_postgres, name, author="system")
    projected = _contradicted(name)
    seeded, declared = referential.classify_divergences(_heads(live_postgres), [projected])
    assert declared == []
    assert seeded, "le semis contradictoire n a pas ete reconnu"

    referential.repair_seeded_concept(live_postgres, projected, actor="tester@example.com")

    repaired = _heads(live_postgres)[name]
    assert repaired["additivity_class"] == "non_additive"
    assert repaired["aggregation"] is None
    assert repaired["version_id"] != first_version
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT additivity_class, version_number FROM app.semantic_concept_versions "
            "WHERE id = %s",
            (first_version,),
        )
        assert cur.fetchone() == ("additive", 1), "la version 1 a ete reecrite"


def test_a_version_somebody_published_is_never_repaired(live_postgres):
    """Une declaration humaine reste une divergence, quoi qu elle contredise.

    La signature du semis est mesurable : version 1, auteur `system`, seule
    version du concept. Un autre auteur, et le concept sort du lot reparable.
    """
    name = _unique_name("declared_metric")
    _seed_like_migration_142(live_postgres, name, author="someone@example.com")
    projected = _contradicted(name)

    seeded, declared = referential.classify_divergences(_heads(live_postgres), [projected])
    assert seeded == []
    assert declared, "la contradiction a disparu du rapport"

    report = referential.provision(
        live_postgres, actor="tester@example.com", repair_seeded=True
    )
    assert name not in report["repaired"]


def test_every_mint_leaves_a_journal_line_naming_its_scope(live_postgres):
    """Minter un concept de PLATEFORME change ce que tout projet lit."""
    concept = referential.PlatformConcept(
        name=_unique_name("journal_metric"),
        value_type="decimal",
        expression={"op": "source_measure", "concept": "journal"},
        additivity_class="additive",
        aggregation={"function": "sum"},
    )
    minted = referential.declare_platform_concept(
        live_postgres, concept, actor="tester@example.com"
    )

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT identity, metadata->>'scope', metadata->>'name', "
            "       metadata->>'derived_from' "
            "  FROM app.audit_log WHERE action = 'platform_semantic_concept.declared' "
            "   AND metadata->>'concept_id' = %s",
            (minted["concept_id"],),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    identity, scope, journalled, derived = rows[0]
    assert (identity, scope, derived) == (
        "tester@example.com",
        "platform",
        "dbt/seeds/dim_metric.csv",
    )
    assert journalled == minted["name"]
