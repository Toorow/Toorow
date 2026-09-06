-- 232 — Un contrat revu tient dans le budget contre lequel on le mesure.
--
-- CE QUE LA MARCHE A MESURE, le 2026-08-08, sur le parcours fondateur.
-- `POST /datastream-setup-drafts/{draft}/final-reviews` refusait, et le refus
-- arretait l'assistant a l'etape 8 sur 9 : `publish-activate` etait hors
-- d'atteinte, donc aucun Datastream de source-fichier ne pouvait etre active,
-- donc rien n'atterrissait. `SURFACE-STATE.md` donnait pourtant 9 etapes sur 9
-- OK -- au sens qu'il declare lui-meme, << batie, routee, couverte par un test
-- qui existe >>, et il previent dans la meme phrase que cela ne dit pas que
-- quelqu'un a parcouru.
--
-- POURQUOI CE N'EST PAS UN PLAFOND QU'ON RELEVE PARCE QU'IL DERANGE. La
-- premiere mesure donnait 14622 octets pour 8192, et la conclusion facile
-- aurait ete de doubler la borne. Elle aurait grave une DUPLICATION dans le
-- contrat : `confirmed_intent_bundle` (7039 o) et `mapping_payload` (5460 o)
-- portaient le meme `profile` et la meme `suggestion` par champ, parce que le
-- second est compile a partir du premier. Le bundle ne voyage plus qu'aminci
-- -- nom, role, grain, nombre de champs, hash -- et le contrat est tombe a
-- 8349 octets. C'est APRES avoir retire les 43 % redondants qu'il reste 157
-- octets au-dessus, et c'est ce qui rend le relevement defendable.
--
-- CE QUI RESTE EST INCOMPRESSIBLE. `mapping_payload` (5873 o) est un document
-- valide par son propre schema, qui declare `profile` et `suggestion` REQUIS
-- sur chaque champ : une tentative de les y referencer au lieu de les porter a
-- ete refutee par ce schema. Et il croit avec le nombre de colonnes -- la
-- mesure ci-dessus est deja prise sur les seules colonnes REQUISES du template
-- `OFFLINE_OOH_V1`. Un fichier media ordinaire depasse donc 8192 par
-- construction.
--
-- 16384 ET PAS PLUS. Le double, ce qui laisse de la marge a un template plus
-- large sans faire de cette colonne un depotoir. La clause qui compte pour la
-- SECURITE -- le refus des secrets par nom -- est reprise mot pour mot : ce
-- n'est pas elle qu'on relache, et la fonction reste IMMUTABLE.
--
-- La 134 n'est pas re-editee : elle est corrigee par celle-ci.

CREATE OR REPLACE FUNCTION app.safe_preconfiguration_evidence(value JSONB)
RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $$
    SELECT jsonb_typeof(value) = 'object'
       AND octet_length(value::text) <= 16384
       AND lower(value::text) !~ '"(credential|secret|access_token|refresh_token|provider_account_id|raw_sample|raw_payload|authorization)"[[:space:]]*:';
$$;
