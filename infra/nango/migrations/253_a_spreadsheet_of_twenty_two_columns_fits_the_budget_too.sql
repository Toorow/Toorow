-- Le budget d'un cliche de revue tenait un template media, pas un tableur.
--
-- MESURE, 2026-08-11, sur le chemin de l'assistant en production, avec un
-- classeur reel de 531 lignes et 22 colonnes (catalogue de videos : identifiant,
-- titre, URL, date, duree, vues, tags, chapitres, description...) :
--
--   Final review snapshot exceeds the safe limit: 16474 bytes for a 16384 byte
--   budget. Heaviest keys: mapping_payload 13817 o, plan_intent 1223 o,
--   acknowledged_warning_ids 277 o.
--
-- Quatre-vingt-dix octets. La revue finale etait refusee, donc la confirmation
-- et l'activation avec elle : aucun Datastream ne pouvait naitre de ce fichier.
--
-- C'EST LE MEME CONSTAT QUE LA 232, UN CRAN PLUS LOIN. Celle-la releve 8192 en
-- 16384 en ecrivant : « `mapping_payload` croit avec le nombre de colonnes [...]
-- un fichier media ordinaire depasse donc 8192 par construction ». La mesure
-- ci-dessus donne le coefficient : 13817 o pour 22 colonnes, soit ~628 o par
-- colonne, et le schema du mapping declare `profile` et `suggestion` REQUIS sur
-- chaque champ -- la 232 avait deja tente de les referencer au lieu de les
-- porter, et son propre schema l'a refute. Un tableur ordinaire depasse donc
-- 16384 par construction, exactement comme un fichier media depassait 8192.
--
-- 65536 ET PAS PLUS. Quatre fois, ce qui couvre une centaine de colonnes au
-- coefficient mesure -- au-dela, ce n'est plus un tableur qu'on decrit, c'est un
-- entrepot, et il a son propre mode (`external_bq`). La clause qui compte pour
-- la SECURITE -- le refus des secrets par nom -- est reprise mot pour mot : ce
-- n'est pas elle qu'on relache, et la fonction reste IMMUTABLE.
--
-- Ni la 134 ni la 232 ne sont re-editees : elles sont corrigees par celle-ci.
-- `core.datastream_activation._FINAL_REVIEW_MAX_BYTES` porte le meme chiffre et
-- doit rester egal : un garde plus large laisse passer une charge que la
-- contrainte refusera en CheckViolation, un garde plus etroit refuse ce que la
-- base accepterait.

CREATE OR REPLACE FUNCTION app.safe_preconfiguration_evidence(value JSONB)
RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $$
    SELECT jsonb_typeof(value) = 'object'
       AND octet_length(value::text) <= 65536
       AND lower(value::text) !~ '"(credential|secret|access_token|refresh_token|provider_account_id|raw_sample|raw_payload|authorization)"[[:space:]]*:';
$$;
