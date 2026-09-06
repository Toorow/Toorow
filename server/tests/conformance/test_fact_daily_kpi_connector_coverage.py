"""Combien de connecteurs atteignent VRAIMENT le fait canonique -- Story 53.10.

CE QUE CE FICHIER EXISTE POUR EMPECHER, et c'est une croyance, pas un bug. Un
ingenieur lit que 38 connecteurs declarent un modele de staging, que les 38 ont
desormais un loader de seed, et que `dbt build` passe. Il en conclut « les
connecteurs sont couverts ». Il a raison sur chaque fait et tort sur la
conclusion : un modele de staging qui se construit et que RIEN ne reference ne
peut pas produire une seule ligne dans `fact_daily_kpi`. Il est vert et
terminal.

CE QUI A RENDU LA CROYANCE CREDIBLE. Trois choses, chacune raisonnable :
la suite de conformite s'arrete a la frontiere du connecteur (elle prouve que le
connecteur rend les bonnes lignes, pas que ces lignes traversent) ; la garde de
`test_seed_to_mart_loop.py` mesure la matiere de seed, ce qui est la bonne
question pour une autre etape ; et les stories connecteur passees `done` ont
inspecte la FORME -- le repertoire existe -- plutot que le contenu.

CE QUE CE FICHIER MESURE, et il le derive du depot a chaque execution :

    modele de staging declare  ->  atteint-il `fact_daily_kpi` ?
                               ->  sinon, atteint-il un autre mart ?
                               ->  sinon, il est TERMINAL.

Aucune liste de connecteurs couverts n'est ecrite ici : elle se recalcule, donc
elle se ferme d'elle-meme le jour ou un modele est branche, et elle se rouvre le
jour ou un connecteur est ajoute sans l'etre. Seul l'ENSEMBLE TERMINAL est
inscrit, et il est a SENS UNIQUE : en retirer un nom est une reparation qui
tient, en ajouter un demande de repondre a la question que ce fichier pose.

« ATTEINDRE » EST TRANSITIF, ET LA PREMIERE VERSION NE L'ETAIT PAS. Elle lisait
les `ref()` DIRECTS de `fact_daily_kpi.sql` et s'arretait la, alors que la
question posee plus haut est « atteint-il le fait canonique ? ». Le fait termine
par `SELECT * FROM {{ ref('int_country_daily_kpi') }}`, et cet intermediaire
reference `stg_dv360_daily` et `stg_taboola_daily` : les deux connecteurs
produisent bel et bien des lignes dans `main_marts.fact_daily_kpi`, et le compte
publie les rangeait « autre mart ». Un saut d'un cran comptait donc un chemin
reel comme une absence -- une surface de verification qui sous-declare sa
couverture est le meme defaut que celle qui la sur-declare.

Mesure du 2026-08-17 (chantier 67-27), et la commande qui la donne est ce fichier
lui-meme (`pytest server/tests/conformance/test_fact_daily_kpi_connector_coverage.py -q -s`) :

    38 connecteurs declarent un modele de staging
    36 atteignent fact_daily_kpi ............................ 95 %
     1 atteint un autre mart et lui seul ................... strava
     1 n'est reference par RIEN ............................ monday

    54 modeles de staging declares
     7 n'atteignent pas le fait, chacun avec sa raison ecrite

Mesure precedente, gardee parce qu'elle explique la forme du fichier : le
2026-08-10, 18 atteignaient le fait (47 %), 3 un autre mart, 17 rien du tout.
AI-270 en a branche 16 les 2026-08-16 et 17 ; youtube-analytics et
google-business-profile l'ont ete par le chantier 67-27.

CE QUE LA STALENESS A COUTE, et c'est la raison d'etre du bloc de mesure
ci-dessus. Le nombre `47 %` a survecu onze jours dans
`docs/product-architecture/caveats-register.md` (CAV-21) apres avoir cesse d'etre
vrai. L'audit connecteurs du 2026-08-17 l'a cite FIDELEMENT depuis ce document,
et un chantier entier a ete ouvert contre un ecart deja referme. Un nombre publie
sans que rien ne force sa republication est un nombre qui ment a date fixe : ce
fichier IMPRIME le sien a chaque execution pour que la republication soit
gratuite, et c'est le document a cote qui doit suivre.

ET LE COMPTE PAR CONNECTEUR CACHE ENCORE AUTRE CHOSE, voir `_MODELS_NOT_IN_FACT`
plus bas : il y a 54 modeles de staging pour 38 connecteurs, et un connecteur
compte pour couvert des qu'UN de ses modeles est branche.

CE COMPTE EST CONFRONTE AU MART REEL, et il ne l'etait pas non plus. Ce fichier
lit du TEXTE SQL ; l'instrument qui tranche est la base construite par
`server/tests/integration/test_seed_to_mart_loop.py`, ou
`SELECT DISTINCT connector FROM main_marts.fact_daily_kpi` rend LE MEME
ENSEMBLE. C'est `test_the_published_coverage_matches_the_mart` la-bas qui tient
les deux ensembles egaux, dans les deux sens -- sans elle, un chiffre faux tire
d'une lecture statique peut etre publie, et il l'a ete.

AUCUN NOMBRE N'EST ECRIT DANS CETTE PHRASE, ET C'EST LA REPARATION. Elle a porte
« rend les memes 18 » jusqu'au 2026-08-24, alors que la base construite en rend
36 (mesure du 2026-08-24, sur le DuckDB bati par `test_seed_to_mart_loop.py`).
Un compte recopie a cote d'une egalite qui, elle, se verifie a chaque
construction, ne peut que perimer : le compte vit dans le bloc de mesure
ci-dessus, qui est date, et dans la sortie que ce fichier IMPRIME.

LES DIX-SEPT D'ALORS N'ETAIENT PAS DES CONNECTEURS MINEURS -- `google-ads`,
`sa360`, `microsoft-ads`, `amazon-ads`, `amazon-dsp`, `pinterest-ads`, `x-ads`,
`thetradedesk`, c'est-a-dire l'essentiel du media paye -- et c'est ce qui rendait
le caveat de l'epic 53 grave plutot que pedant. Les huit sont branches depuis
(AI-270 les 2026-08-16 et 17), et le mart construit les rend TOUS LES HUIT
(`SELECT DISTINCT connector FROM main_marts.fact_daily_kpi`, mesure du
2026-08-24). Ce qui reste hors du fait n'est plus un seau anonyme dont on publie
la taille : il est nomme et signe, connecteur par connecteur dans
`_KNOWN_TERMINAL`, modele par modele dans `_MODELS_NOT_IN_FACT`. Publier « 38
connecteurs » sans ces deux tables, c'est le caveat que l'epic 53 nomme.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULES = _REPO_ROOT / "server" / "modules"
_SHARED_DBT_MODELS = _REPO_ROOT / "dbt" / "models"

#: Le fait canonique est designe par son NOM de modele, pas par son chemin : la
#: cloture remonte le graphe `ref()` et un chemin ne se remonte pas.
_FACT_MODEL = "fact_daily_kpi"

#: `{{ ref('nom') }}` sous ses deux quotes et ses espaces.
_REF = re.compile(r"ref\(\s*['\"]([a-z0-9_]+)['\"]\s*\)")

#: Les connecteurs dont le modele de staging n'est reference par AUCUN modele,
#: partage ou local. SENS UNIQUE : retirer un nom est une reparation ; en
#: ajouter un exige de dire pourquoi ce connecteur se construit vers nulle part.
#:
#: Ce n'est PAS la liste des connecteurs couverts -- celle-la se calcule. C'est
#: la dette acceptee, ecrite pour qu'elle ne puisse pas grandir en silence.
_KNOWN_TERMINAL = frozenset(
    {
        # AI-270 : monday n a PAS sa place dans un fait de KPI journalier, et ce
        # n est pas une dette. Son staging porte l ETAT de tableaux et d elements
        # -- board_id, item_id, column_id, updated_at -- pas une mesure par jour.
        # Son manifeste declare ZERO metrique canonique. Le brancher demanderait
        # d inventer une metrique qu il ne mesure pas.
        "monday",
    }
)


#: LE MEME CLIQUET, MAIS AU GRAIN DU MODELE -- et c'est la moitie qui manquait.
#:
#: CE QUE LES TROIS SEAUX AU-DESSUS NE PEUVENT PAS VOIR. Ils classent un
#: CONNECTEUR, et un connecteur est « couvert » des qu'UN de ses modeles de
#: staging atteint le fait. Un connecteur qui en declare deux, dont un seul est
#: branche, est donc compte comme entierement couvert -- le second se construit,
#: passe ses tests, et ne produit une ligne nulle part. Mesure du 2026-08-17,
#: rendue par ce fichier : 54 modeles de staging pour 38 connecteurs, donc
#: SEIZE modeles que le compte par connecteur ne regarde jamais.
#:
#: Ce n'etait pas theorique. `stg_taboola_history` et `stg_youtube_breakdown`
#: n'etaient references par RIEN, ni fait ni mart, et les deux etaient invisibles
#: : taboola et youtube-analytics comptaient « atteint » grace a leur autre
#: modele. Le cliquet par connecteur ne pouvait pas les nommer, et personne ne
#: les a nommes pendant qu'ils existaient.
#:
#: LA REGLE EST DONC : tout modele de staging qui n'atteint pas `fact_daily_kpi`
#: porte ICI la raison pour laquelle il ne l'atteint pas. « Atteindre un autre
#: mart » n'est PAS une raison -- c'est exactement ce que youtube-analytics et
#: google-business-profile faisaient pendant que leur propre mart ecrivait, dans
#: son en-tete, que le branchement etait « a follow-up, deliberately deferred ».
#: Un troisieme seau silencieux est un endroit ou la dette se repose.
#:
#: SENS UNIQUE dans les deux sens, comme le cliquet par connecteur : un modele
#: qui apparait sans raison fait echouer le test qui l'exige ; un modele branche
#: dont le nom reste ici fait echouer celui qui refuse les entrees perimees.
#:
#: `stg_youtube_breakdown` EST SORTI DE CETTE TABLE le 2026-09-01 (AI-342), et le
#: cliquet a fonctionne comme prevu : le brancher a rendu son entree perimee, et
#: la retirer est la reparation que ce sens unique autorise. Son entree nommait
#: un blocage -- << il faut d'abord une fixture golden de repartition tiree d'une
#: VRAIE reponse de l'API, PAS fabriquee >> -- dont la premisse etait fausse :
#: `golden_pull.json`, le fixture que cette phrase prenait pour etalon, est
#: lui-meme ecrit (chaine `UC_toorow_demo_channel`, `pull_youtube_001`) et non
#: capture. La regle que ce module tient reellement est que la FORME vienne du
#: connecteur, et `golden_breakdown_pull.json` la tient : chaque ligne du landing
#: est derivee par le manifeste, profil par profil.
_MODELS_NOT_IN_FACT: dict[str, str] = {
    "stg_youtube_video_directory": (
        "ANNUAIRE, pas mesure. Une ligne par (jour de releve, chaine, video) qui "
        "porte le TITRE, la date de publication et la duree -- les identites que "
        "les relations analytiques n'ont jamais (elles ne portent que des ids). "
        "Son seul compteur, `lifetime_views`, est un cumul PUBLIC toutes-epoques "
        "lu last-value : le sommer melangerait des eres (AD-4), et les maths de "
        "fenetre recente se derivent de `published_at`, jamais de ce compteur. "
        "Sert la sortie Competitors (capabilities/competitors.md) et nomme le "
        "catalogue propre -- une cle de jointure vers le fait, pas une partition."
    ),
    "stg_ga4_transactions_daily": (
        "GRAIN TRANSACTION, pas grain jour. Une ligne par transaction ne totalise "
        "pas la journee du connecteur : ce n'est pas une partition de breakdown, "
        "c'est une CLE DE JOINTURE. Elle sert `transaction_reconciliation` "
        "(Shopify x GA4 par transaction_id), dont l'en-tete ecrit noir sur blanc "
        "que le grain transaction << NEVER enters fact_daily_kpi >>. Les memes "
        "sessions/conversions GA4 entrent bien dans le fait, par les autres "
        "modeles de staging du connecteur."
    ),
    "stg_gbp_review": (
        "NON ADDITIF. `location_average_rating` et `location_total_review_count` "
        "sont des NIVEAUX courants de la fiche, lus last-value, et "
        "`new_reviews_avg_star_rating` est une moyenne de notes 1..5 -- sommer "
        "l'une ou l'autre rend un nombre sur aucune echelle (AD-4). Seul "
        "`new_reviews` est un flux additif, et il n'a de sens qu'a cote de la "
        "ponderation qui vit dans la meme ligne de `fact_gbp_review_rollup`."
    ),
    "stg_gbp_search_keyword_monthly": (
        "GRAIN MOIS. La source ne publie aucun detail journalier des mots-cles ; "
        "repartir un mois sur ses jours inventerait la distribution, et toute "
        "reconciliation ensuite serait de l'arithmetique sur une fiction. Porte "
        "aussi `is_thresholded` (<< moins de N >>), une BORNE et non une valeur, "
        "que le fait additif n'a aucun moyen de representer."
    ),
    "stg_monday_board_snapshot": (
        "ZERO METRIQUE CANONIQUE. Porte l'ETAT de tableaux et d'elements -- "
        "board_id, item_id, column_id, updated_at -- pas une mesure par jour. Le "
        "brancher demanderait d'inventer une metrique que le connecteur ne prend "
        "pas. Seul connecteur entierement terminal, et ce n'est pas une dette."
    ),
    "stg_strava_club_daily": (
        "NON ADDITIF. `member_count` et `following_count` sont des NIVEAUX "
        "ponctuels (`aggregation_rule=latest`) ; le fait central est additif-seul "
        "(SUM) et un niveau n'y a aucune ligne honnete -- le precedent exact est "
        "`average_position` de GSC, qui vit dans une vue semantique. La tendance "
        "est member_count(t) - member_count(t-1), derivee a la carte."
    ),
    "stg_taboola_history": (
        "FLUX D'EVENEMENTS, pas de mesures. `raw_taboola_history` porte des "
        "enregistrements de CHANGEMENT de campagne (un `change_type` par ligne), "
        "que `transform_events()` projette en evenements canoniques -- le chemin "
        "evenements, qui ne passe pas par le fait de KPI. Les metriques taboola "
        "entrent bien dans le fait, par `stg_taboola_daily`. "
        "N'ETAIT NOMME NULLE PART avant le 2026-08-17 : le cliquet par connecteur "
        "voyait taboola << atteint >> et ne pouvait pas regarder ce modele-ci."
    ),
}


def _staging_models_by_connector() -> dict[str, set[str]]:
    """connecteur -> noms de ses modeles de staging, lus sur le disque."""
    found: dict[str, set[str]] = {}
    for path in _MODULES.glob("*/dbt/staging/stg_*.sql"):
        connector = path.parent.parent.parent.name
        found.setdefault(connector, set()).add(path.stem)
    return found


def _every_dbt_model() -> list[Path]:
    """Tout modele dbt du depot, partage ou local a un connecteur.

    DEUX RACINES NOMMEES, PAS UN `rglob` SUR LE DEPOT. La premiere version
    balayait `_REPO_ROOT.rglob("*.sql")` en filtrant ensuite : elle traversait
    `dbt/target/`, `node_modules/` et `.venv/`, et le fichier mettait **265
    secondes**. Les deux racines ci-dessous donnent le meme ensemble en une
    fraction de seconde, et `server/pyproject.toml` plafonne les tests a 180 s --
    la version lente n'aurait pas survecu a son propre plafond ailleurs.

    `target/` reste exclu par construction : ce sont les rendus COMPILES, ou
    chaque `ref()` a deja ete remplace par un nom de relation. Les compter
    reviendrait a lire deux fois le meme modele, et a manquer ceux qui n'ont
    jamais ete compiles ici.
    """
    seen: list[Path] = []
    if _SHARED_DBT_MODELS.is_dir():
        seen.extend(p for p in _SHARED_DBT_MODELS.rglob("*.sql"))
    for module_dbt in _MODULES.glob("*/dbt"):
        seen.extend(p for p in module_dbt.rglob("*.sql") if "target" not in p.parts)
    return seen


def _refs_by_model() -> dict[str, set[str]]:
    """modele -> les modeles qu'il cite. Le graphe `ref()` du depot, une fois lu."""
    graph: dict[str, set[str]] = {}
    for path in _every_dbt_model():
        cited = set(_REF.findall(path.read_text(encoding="utf-8", errors="replace")))
        graph.setdefault(path.stem, set()).update(cited)
    return graph


def _referenced_models() -> set[str]:
    """Tout modele cite par un `ref()` quelque part."""
    referenced: set[str] = set()
    for cited in _refs_by_model().values():
        referenced |= cited
    return referenced


def direct_fact_refs() -> set[str]:
    """Les `ref()` ECRITS DANS `fact_daily_kpi.sql`, et rien d'autre.

    Expose parce qu'un test plus bas s'en sert pour prouver que la classification
    ne s'arrete PAS ici. Ce n'etait pas un detail d'implementation : c'etait la
    definition de « atteint » jusqu'au 2026-08-10, et elle sous-comptait de deux.
    """
    return set(_refs_by_model().get(_FACT_MODEL, set()))


def models_reaching_fact() -> set[str]:
    """La CLOTURE TRANSITIVE des dependances de `fact_daily_kpi`.

    Un modele de staging atteint le fait des qu'un chemin de `ref()` l'y mene,
    quelle que soit sa longueur. `fact_daily_kpi` -> `int_country_daily_kpi` ->
    `stg_dv360_daily` est un chemin de longueur 2, et les lignes dv360 sont dans
    le mart : les compter absentes serait faux, et ca l'a ete.

    `seen` rend la marche insensible aux cycles -- dbt les refuse, mais un
    parcours qui boucle sur un depot casse est une session qui ne rend jamais la
    main, pas un test rouge.
    """
    graph = _refs_by_model()
    assert _FACT_MODEL in graph, (
        f"aucun modele nomme {_FACT_MODEL!r} sous les deux racines lues : le fait "
        "canonique a ete deplace ou renomme, et toute la classification en depend"
    )
    reached: set[str] = set()
    frontier = list(graph[_FACT_MODEL])
    while frontier:
        name = frontier.pop()
        if name in reached:
            continue
        reached.add(name)
        frontier.extend(graph.get(name, set()))
    return reached


def models_not_reaching_fact() -> dict[str, str]:
    """modele de staging -> son connecteur, pour tout modele hors du fait.

    Derive du disque comme tout le reste : aucune appartenance n'est ecrite. Le
    grain est le MODELE et non le connecteur, ce qui est tout l'objet de cette
    couche -- voir `_MODELS_NOT_IN_FACT`.
    """
    fact = models_reaching_fact()
    return {
        model: connector
        for connector, models in _staging_models_by_connector().items()
        for model in models
        if model not in fact
    }


def classify() -> dict[str, set[str]]:
    """Les trois seaux, derives. Aucune appartenance n'est ecrite a la main."""
    staging = _staging_models_by_connector()
    fact = models_reaching_fact()
    referenced = _referenced_models()

    in_fact, other_mart, terminal = set(), set(), set()
    for connector, models in staging.items():
        if models & fact:
            in_fact.add(connector)
        elif models & referenced:
            other_mart.add(connector)
        else:
            terminal.add(connector)
    return {
        "declared": set(staging),
        "in_fact": in_fact,
        "other_mart": other_mart,
        "terminal": terminal,
    }


def test_the_coverage_ratio_is_published_rather_than_implied(capsys):
    """Le nombre est IMPRIME, pas seulement verifie.

    Toute l'histoire de cette story est un nombre qu'on a cru sans commande.
    L'imprimer sous `-s` donne la commande, et l'assertion en dessous garantit
    qu'il reste vrai.
    """
    buckets = classify()
    declared = len(buckets["declared"])
    reached = len(buckets["in_fact"])
    ratio = reached / declared if declared else 0.0

    with capsys.disabled():
        print()
        print(f"  connecteurs declarant un modele de staging : {declared}")
        print(f"  atteignant fact_daily_kpi                  : {reached}  ({ratio:.0%})")
        print(f"  atteignant un autre mart, et lui seul      : {len(buckets['other_mart'])}"
              f"  {sorted(buckets['other_mart'])}")
        print(f"  references par RIEN                        : {len(buckets['terminal'])}"
              f"  {sorted(buckets['terminal'])}")

    assert declared > 0, "aucun modele de staging trouve : le calcul lit le mauvais chemin"
    assert reached > 0, "aucun connecteur n'atteint le fait canonique : lecture cassee"
    # La somme doit fermer, sinon un connecteur est tombe entre deux seaux et le
    # ratio publie omettrait quelqu'un -- exactement le defaut vise.
    assert reached + len(buckets["other_mart"]) + len(buckets["terminal"]) == declared


def test_reaching_the_fact_is_transitive_rather_than_one_hop():
    """Un connecteur atteint par un INTERMEDIAIRE compte comme atteignant le fait.

    C'est le defaut qui a produit le faux 16 : la classification lisait les
    `ref()` directs de `fact_daily_kpi.sql` et rangeait « autre mart » deux
    connecteurs dont les lignes sont dans le fait. Aucun nom n'est ecrit ici --
    la garde compare les deux definitions de « atteint » et exige que la seconde
    l'emporte pour tout modele que la cloture rejoint.

    Le jour ou plus aucun modele n'entre par un intermediaire, cette garde passe
    sans rien dire, et c'est correct : elle verifie une regle de classement, elle
    ne fige pas la forme du graphe.

    ELLE DERIVE SON TEMOIN ELLE-MEME, et la premiere version ne le faisait pas :
    elle calculait `models_reaching_fact() - direct_fact_refs()`, donc rendre la
    fonction non transitive vidait l'ensemble teste et la garde passait au VERT
    sur le defaut qu'elle existe pour attraper. Mesure du 2026-08-10 : la mutation
    « un seul cran » laissait ce fichier a 5 passed. Le second cran est desormais
    lu directement dans le graphe -- un cran de plus que la version rejetee suffit
    a la faire tomber, et ce n'est pas une copie de la cloture : si les deux
    partageaient le meme calcul, une erreur commune resterait invisible.
    """
    staging = _staging_models_by_connector()
    buckets = classify()
    graph = _refs_by_model()
    direct = direct_fact_refs()
    second_hop = set()
    for name in direct:
        second_hop |= graph.get(name, set())
    through_intermediate = second_hop - direct

    misfiled = sorted(
        connector
        for connector, models in staging.items()
        if models & through_intermediate and connector not in buckets["in_fact"]
    )
    assert not misfiled, (
        "connecteur(s) dont un modele de staging atteint `fact_daily_kpi` par un "
        "modele intermediaire et que la classification compte ailleurs :\n  "
        + "\n  ".join(misfiled)
        + "\n\nLa question posee est « atteint-il le fait canonique ? », pas "
        "« est-il cite dans son fichier ? ». Un saut d'un cran sous-compte la "
        "couverture, et un compte publie trop bas est aussi faux qu'un trop haut."
    )


def test_no_connector_becomes_terminal_without_being_named():
    """La dette ne grandit pas en silence.

    Un connecteur ajoute avec un modele de staging et aucun aval tombe ICI, avec
    son nom, plutot que d'etre absorbe dans un compte global qui continue
    d'afficher << 38 connecteurs >>.
    """
    terminal = classify()["terminal"]
    newly = sorted(terminal - _KNOWN_TERMINAL)
    assert not newly, (
        "connecteur(s) dont le modele de staging ne mene nulle part et qui ne sont "
        "pas inscrits comme dette :\n  " + "\n  ".join(newly) + "\n\n"
        "Soit le modele est branche vers un mart, soit son nom rejoint "
        "_KNOWN_TERMINAL avec la raison. Un modele de staging vert et terminal ne "
        "produit aucune ligne, et le compte de connecteurs le dit couvert."
    )


def test_the_terminal_list_shrinks_and_never_stales():
    """Un nom reste dans la dette tant qu'il la porte, et pas une execution de plus.

    Sans cette moitie, la liste ne se viderait jamais : un connecteur branche
    resterait inscrit comme terminal, et la prochaine lecture croirait la dette
    plus grande qu'elle n'est. Le cliquet tourne dans les deux sens -- refus
    d'ajout au-dessus, refus de perime ici.
    """
    terminal = classify()["terminal"]
    repaired = sorted(_KNOWN_TERMINAL - terminal)
    assert not repaired, (
        "connecteur(s) desormais branches mais toujours inscrits comme dette :\n  "
        + "\n  ".join(repaired)
        + "\n\nRetire-les de _KNOWN_TERMINAL : la reparation doit compter."
    )


def test_every_staging_model_outside_the_fact_says_why(capsys):
    """LA GARDE DU CHANTIER 67-27 : plus un seul modele muet.

    La question que ce fichier posait -- << ce connecteur atteint-il le fait ? >>
    -- se pose desormais MODELE par MODELE, parce que c'est au grain du modele
    que la dette se cachait : un connecteur a deux stagings dont un seul est
    branche compte pour couvert, et le second n'existe pour personne.

    Un modele qui n'atteint pas `fact_daily_kpi` doit porter sa raison dans
    `_MODELS_NOT_IN_FACT`. La couverture cesse alors d'etre un pourcentage : elle
    devient une liste ou chaque absence est signee.
    """
    outside = models_not_reaching_fact()
    unexplained = sorted(set(outside) - set(_MODELS_NOT_IN_FACT))

    with capsys.disabled():
        print()
        print(f"  modeles de staging declares       : "
              f"{sum(len(v) for v in _staging_models_by_connector().values())}")
        print(f"  hors de fact_daily_kpi, expliques : {len(outside) - len(unexplained)}")
        for model, connector in sorted(outside.items()):
            print(f"      {model:38s} ({connector})")

    assert not unexplained, (
        "modele(s) de staging qui n'atteignent pas `fact_daily_kpi` et dont "
        "personne ne dit pourquoi :\n  " + "\n  ".join(unexplained) + "\n\n"
        "Soit le modele est branche vers le fait, soit son nom rejoint "
        "_MODELS_NOT_IN_FACT avec la RAISON. « Il atteint un autre mart » n'en "
        "est pas une : c'est precisement ce que faisaient youtube-analytics et "
        "google-business-profile pendant que leur propre mart ecrivait que le "
        "branchement etait un follow-up differe."
    )


def test_a_reason_that_is_not_a_reason_does_not_pass():
    """Une raison est un ARGUMENT, pas un marqueur.

    Sans cette moitie, `_MODELS_NOT_IN_FACT` se remplirait de `"TODO"` et le test
    au-dessus passerait au vert sur exactement la dette qu'il existe pour rendre
    visible -- le defaut que ce depot appelle un faux vert. Le seuil ne juge pas
    la prose : il refuse ce qui n'est manifestement pas une explication.
    """
    too_thin = sorted(
        model for model, reason in _MODELS_NOT_IN_FACT.items() if len(reason.strip()) < 80
    )
    assert not too_thin, (
        "entree(s) de _MODELS_NOT_IN_FACT dont la raison n'explique rien :\n  "
        + "\n  ".join(too_thin)
        + "\n\nCe champ est lu par la personne qui se demandera pourquoi ce "
        "connecteur ne produit aucune carte. Un marqueur ne lui repond pas."
    )


def test_no_model_stays_listed_once_it_is_wired():
    """Le cliquet du grain modele tourne dans les deux sens.

    Le jumeau exact de `test_the_terminal_list_shrinks_and_never_stales`, une
    couche plus bas : une raison qui survit au branchement ferait croire la dette
    plus grande qu'elle n'est, et la prochaine lecture chercherait un probleme
    resolu.
    """
    outside = models_not_reaching_fact()
    stale = sorted(set(_MODELS_NOT_IN_FACT) - set(outside))
    assert not stale, (
        "modele(s) desormais dans le fait mais toujours inscrits comme hors de "
        "lui :\n  " + "\n  ".join(stale)
        + "\n\nRetire-les de _MODELS_NOT_IN_FACT : la reparation doit compter."
    )


def test_the_declared_set_is_the_one_the_seed_guard_uses():
    """Deux gardes, une seule definition de << l'ensemble declare >>.

    `test_seed_to_mart_loop.py` derive le meme ensemble pour decider s'il peut
    tourner. Si les deux divergeaient, l'une dirait << 38 couverts >> pendant que
    l'autre en compterait 34, et le lecteur croirait celle qui l'arrange. Ce test
    ne duplique pas la derivation : il verifie qu'elles rendent la meme chose.
    """
    here = classify()["declared"]
    modules_dir = _REPO_ROOT / "server" / "modules"
    there = {
        path.parent.parent.parent.name for path in modules_dir.glob("*/dbt/staging/stg_*.sql")
    }
    assert here == there, (
        "les deux gardes ne parlent plus du meme ensemble de connecteurs : "
        f"ici {len(here)}, la-bas {len(there)}, difference {sorted(here ^ there)}"
    )
