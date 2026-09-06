-- 280 -- Un ticket scelle peut enfin etre dechire (67-15d).
--
-- LE DEFAUT, EN UNE PHRASE. La session navigateur est un ticket Fernet scelle
-- SANS AUCUN ETAT SERVEUR : `browser_oidc.py:574-596` le mint, `:610-639` le
-- rouvre, et rien entre les deux ne consulte la base. Le logout
-- (`browser_oidc.py:662-668`) se contente de `delete_cookie` -- il demande au
-- navigateur d'oublier le ticket, il ne le rend pas invalide. Une copie du
-- cookie continue d'ouvrir `/api/me` pendant les 8 heures du TTL
-- (`TOOROW_OIDC_SESSION_TTL_SECONDS`, defaut 28800, `:125-134`). Suspendre ou
-- retirer un membre coupe ses gardes DB mais PAS sa session : les lectures qui
-- ne touchent pas `org_members` restent servies jusqu'a l'expiration. Nomme par
-- `reviews/audit-2026-08-17/12-acces-utilisateurs.md:286-290`.
--
-- LE MODELE EXISTE DEJA DANS LE DEPOT, et ce n'est pas une invention de cette
-- migration. `render_shares.resolve_session` (`render_shares.py:865-906`)
-- revalide le Share VIVANT a CHAQUE appel, et son propre commentaire dit
-- pourquoi : « une revocation qui ne fermait la porte qu'aux nouveaux echanges
-- laisserait tourner toute session deja emise jusqu'a son expiration -- ce qui
-- n'est pas ce que "la revocation coupe l'acces immediatement" veut dire pour
-- la personne qui a clique revoquer ». Meme philosophie ici : une session
-- revoquee est refusee A L'APPEL SUIVANT, pas a l'expiration.
--
-- DEUX PORTEES, ET AUCUNE N'EST DE CONFORT. Elles repondent a deux questions
-- que la meme table ne pourrait pas confondre sans mentir :
--
--   scope='session'    UN ticket exact, designe par son `sid`. C'est le logout
--                      reel : je ferme CETTE fenetre, mes autres restent
--                      ouvertes.
--
--   scope='principal'  TOUTES les sessions d'une personne emises AVANT
--                      `revoked_at` -- une borne « not before », pas une liste.
--                      C'est la seule forme qui marche a la sortie d'org : le
--                      serveur ne tient aucun registre des tickets emis, il ne
--                      PEUT pas les enumerer. Et parce que la borne est
--                      temporelle, une reconnexion legitime posterieure passe :
--                      revoquer n'est pas bannir.
--
-- POURQUOI PAS DE REGISTRE DES SESSIONS EMISES. Il faudrait une ecriture par
-- login et un balayage des lignes expirees, pour repondre a une question que la
-- borne temporelle repond deja sans etat. Le ticket reste la source de verite
-- de ce qu'il affirme ; la table ne dit qu'une chose, et seulement quand elle a
-- quelque chose a dire : « ceci a ete coupe ».
--
-- LE COUT SUR LE CHEMIN CHAUD. Une seule requete par requete authentifiee, deux
-- sondes d'index sous un BitmapOr, aucune ligne lue dans le cas nominal (la
-- table est vide ou minuscule). Les deux index partiels ci-dessous existent
-- exactement pour ca : sans eux la sonde `principal` degenererait en Seq Scan a
-- mesure que la table grossit.
--
-- PAS DE `org_id`, ET C'EST DELIBERE. Une revocation de portee `principal`
-- traverse les organisations par construction -- la personne sortie d'une org
-- peut etre membre d'une autre, et la coupure porte sur SON ticket, pas sur un
-- locataire. La table sort donc du perimetre du cliquet de la migration 273
-- (`test_rls_covers_every_org_scoped_table_pg.py`), qui enumere les tables
-- portant `org_id` : elle n'en porte pas, elle n'y entre pas, et rien n'est
-- exempte en douce.
--
-- EFFACEMENT RGPD. La table n'est PAS append-only : aucun trigger n'interdit le
-- DELETE, et `connector` recoit le privilege DELETE ci-dessous. L'echappatoire
-- de la migration 198/277 n'a donc rien a porter ici -- une purge d'org ou de
-- personne supprime ses lignes par un DELETE ordinaire.

BEGIN;

CREATE TABLE IF NOT EXISTS app.revoked_browser_sessions (
    id          TEXT        PRIMARY KEY,
    scope       TEXT        NOT NULL
                CHECK (scope IN ('session', 'principal')),
    -- Le `sid` du ticket scelle. Present pour 'session', absent pour
    -- 'principal' : une borne « not before » ne designe aucun ticket.
    session_id  TEXT
                CHECK (session_id IS NULL OR length(session_id) BETWEEN 8 AND 128),
    -- '*' = tout emetteur. La porte de sortie d'org ne connait qu'une identite
    -- applicative, pas l'emetteur qui a signe le ticket ; la porte `me` connait
    -- le sien exactement et l'inscrit.
    issuer      TEXT        NOT NULL
                CHECK (issuer = BTRIM(issuer) AND length(issuer) BETWEEN 1 AND 2048),
    subject     TEXT        NOT NULL
                CHECK (subject = BTRIM(subject) AND length(subject) BETWEEN 1 AND 2048),
    revoked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_by  TEXT        NOT NULL
                CHECK (revoked_by = BTRIM(revoked_by) AND length(revoked_by) BETWEEN 1 AND 2048),
    reason      TEXT        NOT NULL
                CHECK (reason = BTRIM(reason) AND length(reason) BETWEEN 1 AND 200),
    CHECK ((scope = 'session') = (session_id IS NOT NULL))
);

-- Sonde 1 : ce ticket exact a-t-il ete dechire. UNIQUE plutot qu'index simple
-- pour que deux logouts concurrents sur le meme ticket ne posent qu'une ligne.
CREATE UNIQUE INDEX IF NOT EXISTS revoked_browser_sessions_session_id
    ON app.revoked_browser_sessions (session_id)
    WHERE session_id IS NOT NULL;

-- Sonde 2 : cette personne a-t-elle une borne posterieure a l'emission du
-- ticket. `subject` d'abord : il est bien plus selectif que `issuer`, dont la
-- valeur est '*' ou l'unique emetteur du deploiement.
CREATE INDEX IF NOT EXISTS revoked_browser_sessions_principal
    ON app.revoked_browser_sessions (subject, revoked_at DESC)
    WHERE scope = 'principal';

COMMENT ON TABLE app.revoked_browser_sessions IS
    'Sessions navigateur coupees avant leur expiration. Consultee par '
    'core.session_revocation.is_session_revoked a chaque requete authentifiee.';

GRANT SELECT, INSERT, DELETE ON app.revoked_browser_sessions TO connector;

COMMIT;
