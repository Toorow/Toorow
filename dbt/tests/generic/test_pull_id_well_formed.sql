-- Generic dbt test: mart pull_id must be well-formed (AD-7), source-agnostic.
--
-- Story 3.6 (FR2): the previous integrity gate was a `relationships` test to a
-- single raw table (raw_ga4_standard_daily) and therefore broke as soon as a
-- second connector (with a different raw table) landed rows in the mart. A
-- per-module central edit would be required for every new source.
--
-- Provenance is already guaranteed structurally: each module's staging model
-- selects pull_id straight from its own raw table (source()), and the mart only
-- MAX()es that value -- a mart pull_id can only originate from some module's raw
-- table. This source-agnostic well-formedness check (prefix + non-null) preserves
-- the AD-7 provenance intent without coupling the central mart to any one raw
-- table. Adding module N+1 needs NO edit here.
--
-- 2026-08-22 (story 69.2) : un DEUXIEME prefixe est legitime. Le chemin fichier
-- n'a pas de pull -- il a une EXECUTION, `dse_<ULID>`, et c'est exactement le
-- meme fait de provenance sous un autre nom (le mart `managed_feed_superseding`
-- ordonne dessus comme les stagings connecteurs ordonnent sur `pull_id DESC`).
-- Le refuser aurait force l'une de deux faussetes : un `pull_id` invente pour
-- un fichier, ou une branche du mart sans provenance. La forme reste VERIFIEE :
-- deux prefixes connus, jamais << n'importe quoi de non nul >>.
--
-- 2026-08-24 (AI-314) : CE GARDE NE POUVAIT PAS TOURNER EN PRODUCTION. `LIKE ...
-- ESCAPE` n'existe pas en BigQuery, qui repond *Syntax error: Illegal escape
-- sequence: \_* -- donc le seul controle de provenance du fait etait vert en
-- local et REFUSE sur le moteur du nocturne, ou un test refuse est un code de
-- sortie, et un code de sortie est un projet sans marts. Mesure par un dry run
-- BigQuery du test compile (0 octet, gratuit). Le prefixe se compare desormais
-- par sa longueur (`SUBSTR`, que les deux moteurs portent -- verifie sur les deux
-- le meme jour), qui n'a besoin d'aucun echappement et dit la meme chose : les
-- deux prefixes connus, jamais << n'importe quoi de non nul >>. Et pas
-- `NOT LIKE 'pull_%'` sans echappement : `_` y est un joker, donc `pullX_...`
-- passerait le garde.
{% test pull_id_well_formed(model, column_name) %}
SELECT *
FROM {{ model }}
WHERE {{ column_name }} IS NULL
   OR (SUBSTR({{ column_name }}, 1, 5) <> 'pull_'
       AND SUBSTR({{ column_name }}, 1, 4) <> 'dse_')
{% endtest %}
