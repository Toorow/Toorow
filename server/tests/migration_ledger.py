"""Rejouer une migration deja appliquee DEFAIT les reparations qui l ont suivie.

Un helper partage, dans `tests/` comme `english_guard`, parce que les deux
familles de tests en ont besoin : `tests/core` et `tests/integration`.

DEUXIEME SYMPTOME DE LA MEME CLASSE, mesure le 2026-09-02, et il est plus cher
que le premier. Les migrations `030`, `032`, `042`, `077`, `078` et `081` sont
ANTERIEURES a la `099`, et chacune ouvre par
`DROP TRIGGER IF EXISTS ... ; CREATE TRIGGER ...`. Les rejouer contre une base
vivante jette la clause `WHEN (current_setting('app.rgpd_erasure', ...))` que la
099 leur avait donnee -- exactement le mecanisme que la 278 documente pour la
267, sauf que l agent est ici une fixture et non une migration.

Consequence : sept gardes DELETE de l arbre org cessent de ceder a un effacement
RGPD -- `csv_excel_import_contracts`, `datastream_mapping_versions`,
`datastream_plan_versions`, `datastream_publication_log`,
`managed_feed_import_ledger`, `managed_feed_rejected_rows`,
`file_source_templates`. Rejeu propre 001->338 dans une base neuve : les sept
portent la clause. Base de travail apres les suites d integration : aucune.
Le plan de pre-deploiement a lu ce rouge comme un defaut du schema livre ; c est
la copie que l instrument mesurait.

`rearm_the_erasure_hatch` ci-dessous repare ce que la 339 repare en base, pour
les deux tests dont le SUJET est le rejeu et qui ne peuvent donc pas s en
passer.
"""

from __future__ import annotations

import pathlib

#: Le meme predicat que la migration 339, DERIVE et non liste : tout garde DELETE
#: pose sur une table que l arbre org atteint, sans l echappatoire ni dans le
#: trigger ni dans le corps de sa fonction.
_REARM_SQL = """
DO $rearm$
DECLARE
    guard CONSTANT text :=
        ' WHEN (current_setting(''app.rgpd_erasure'', true) IS DISTINCT FROM ''on'') ';
    rec record;
    new_def text;
BEGIN
    -- Une base nue n a pas encore d organisations : il n y a alors pas d arbre
    -- org, donc rien a reparer, et `::regclass` leverait au lieu de le dire.
    IF to_regclass('app.organizations') IS NULL THEN
        RETURN;
    END IF;

    FOR rec IN
        WITH RECURSIVE tree AS (
            SELECT 'app.organizations'::regclass AS oid
            UNION
            SELECT k.conrelid
            FROM pg_constraint k
            JOIN tree ON k.confrelid = tree.oid
            WHERE k.contype = 'f' AND k.conrelid <> k.confrelid
        )
        SELECT c.relname AS tbl, t.tgname, pg_get_triggerdef(t.oid) AS def
        FROM tree
        JOIN pg_class c ON c.oid = tree.oid
        JOIN pg_trigger t ON t.tgrelid = c.oid
        WHERE c.relnamespace = 'app'::regnamespace
          AND NOT t.tgisinternal
          AND (t.tgtype & 8) > 0
          AND pg_get_triggerdef(t.oid) NOT ILIKE '%rgpd_erasure%'
          AND pg_get_functiondef(t.tgfoid) NOT ILIKE '%rgpd_erasure%'
    LOOP
        CONTINUE WHEN rec.def ILIKE '% WHEN %';
        new_def := regexp_replace(
            rec.def, '\\s+EXECUTE (FUNCTION|PROCEDURE)\\s', guard || 'EXECUTE \\1 '
        );
        CONTINUE WHEN new_def = rec.def;
        EXECUTE format('DROP TRIGGER %I ON app.%I', rec.tgname, rec.tbl);
        EXECUTE new_def;
    END LOOP;
END
$rearm$;
"""


def rearm_the_erasure_hatch(conn) -> None:
    """Rendre a chaque garde DELETE de l arbre org l echappatoire RGPD.

    A APPELER APRES UN REJEU DELIBERE, c est-a-dire dans les deux seuls tests
    dont l assertion EST la reapplication d un fichier de migration
    (`test_reapplying_080_is_idempotent`, `test_081_replays_on_a_populated_log`).
    Partout ailleurs la reponse est `apply_migrations_absent_from_the_ledger`,
    qui ne rejoue rien et n a donc rien a reparer.

    Silencieux et idempotent : sur une base saine la boucle ne trouve rien.
    """
    with conn.cursor() as cur:
        cur.execute(_REARM_SQL)
    conn.commit()


def apply_migrations_absent_from_the_ledger(conn, paths) -> list[str]:
    """Appliquer une migration SEULEMENT si le ledger ne la porte pas deja.

    LE DEFAUT QUE CECI FERME, mesure le 2026-08-17. Vingt-neuf fichiers de test
    rejouent une chaine de migrations dans leur fixture -- une habitude d avant
    le cluster jetable, quand la base de test n etait pas migree. (Re-mesure le
    2026-09-02 par parcours AST des appels `execute(<...>.read_text(...))` :
    **28 fichiers**, dont un seul passait alors par ce helper. Les huit qui
    jettent une echappatoire RGPD sont convertis dans le meme commit que la
    migration 339 ; les dix-neuf autres rejouent des migrations qui ne creent
    aucun garde DELETE de l arbre org et restent a convertir.) Les migrations
    sont ecrites idempotentes (`CREATE TABLE IF NOT EXISTS`,
    `CREATE OR REPLACE FUNCTION`), donc les rejouer << marche >> ... et REMPLACE
    les corps de fonction par leur version d origine.

    Concretement : `sync_external_dispatch_excluded` est corrigee par les
    migrations 103 puis 226 (`COALESCE(..., FALSE)`). Un test qui rejoue
    `023_datastreams.sql` y remet le corps de la 076 --
    `NEW.source_kind = 'external_bq'`, qui rend NULL quand `source_kind` est NULL
    -- pour TOUTE la session pytest. Les 113 `NotNullViolation` qui suivent
    frappent des fichiers qui n ont rien demande, et la victime n est jamais le
    test fautif. C est la forme la plus couteuse d un defaut : celle ou le
    coupable est vert.

    Sur un cluster deja migre la chaine est donc REDONDANTE et NUISIBLE : elle ne
    cree rien qui n existe, et elle defait des reparations. Sur une base nue elle
    reste necessaire, et ce helper l applique.

    Rend la liste de ce qui a ete applique -- vide quand tout etait deja la.
    """
    applied: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM toorow_meta.schema_migrations "
                        "WHERE status = 'applied'")
            known = {str(row[0]) for row in cur.fetchall()}
    except Exception:
        #  Pas de ledger = base nue : tout appliquer, ce qui est le cas pour
        #  lequel ces chaines ont ete ecrites.
        conn.rollback()
        known = set()

    with conn.cursor() as cur:
        for path in paths:
            path = pathlib.Path(path)
            if not path.exists() or path.name in known:
                continue
            cur.execute(path.read_text(encoding="utf-8"))
            applied.append(path.name)
    conn.commit()

    # Une base nue recoit ici des migrations ANTERIEURES a la 099 : la chaine
    # cree alors des gardes DELETE sans l echappatoire, et rien plus loin ne la
    # leur donnera puisque la 099 n est pas dans la chaine. Sur une base migree
    # `applied` est vide et ceci ne fait rien.
    if applied:
        rearm_the_erasure_hatch(conn)
    return applied
