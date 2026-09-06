-- Ce qu'un agent peut RENDRE a la base gouvernee -- et qui n'avait nulle part ou vivre.
--
-- Trois manques mesures le 2026-08-03, tous de la meme famille : le produit
-- CALCULE le signal et ne le GARDE jamais, ou l'accepte sans pouvoir dire d'ou
-- il vient. Une observation isolee est une anecdote ; c'est la RECURRENCE qui
-- fait un travail a faire, et elle exige d'etre ecrite quelque part.
--
-- 1. AI-152 -- UN LIEN PROPOSE PAR UNE MACHINE NE POUVAIT PAS EXISTER.
--    `mdm_business_links.link_origin` n'acceptait que 'direct', et `create_link`
--    l'ecrivait en dur. Un agent qui comble un lien manquant n'avait que deux
--    issues : ne rien ecrire, ou ecrire un lien INDISCERNABLE d'une decision
--    humaine. La seconde est pire que le trou -- elle efface la difference entre
--    << quelqu'un a decide >> et << une machine a devine >>, qui est la seule
--    chose qui rend l'arbre digne de confiance.
--    `context_path_resolutions` connaissait deja `derived` : la valeur existait
--    une couche au-dessus.
--
-- 2. AI-155 -- UNE REMARQUE SUR UNE SKILL PARTAIT DANS UN JOURNAL D'AUDIT.
--    `POST /api/context/nodes/{id}/request-review` accepte une note libre et
--    ecrit une ligne `context.review_requested`. Aucune table, aucun lecteur :
--    `review_requested` n'apparaissait nulle part hors du handler. Un journal
--    d'audit est append-only et scelle ; ce n'est pas une file de travail.
--
-- 3. AI-153 -- LE SORT DES CANDIDATS ETAIT CALCULE A CHAQUE RECHERCHE ET JETE.
--    Chaque marche porte `rejected` / `out_of_scope` / `below_cutoff` pour tout
--    ce qu'elle a atteint, et `emit_walk` ne poste rien sans `progressToken`.
--    Un candidat rejete a repetition pour un metier auquel il n'est pas lie est
--    un RECLASSEMENT a proposer -- derive de l'usage, pas d'une relecture.
--
--    ⚠️ AGREGAT, PAS JOURNAL. Une ligne par (projet, candidat, raison), avec un
--    compteur. Le volume est borne par la taille du corpus, jamais par le trafic
--    -- un journal de chaque rejet de chaque recherche serait ingerable et
--    n'apporterait rien de plus : ce qui compte est combien de fois, pas quand.

-- 1 ------------------------------------------------------------------------

ALTER TABLE app.mdm_business_links
    DROP CONSTRAINT IF EXISTS mdm_business_links_link_origin_check;
ALTER TABLE app.mdm_business_links
    ADD CONSTRAINT ck_mdm_business_links_link_origin
    CHECK (link_origin IN ('direct', 'derived'));

COMMENT ON COLUMN app.mdm_business_links.link_origin IS
    'direct = quelqu''un a decide ; derived = une machine a propose et un humain '
    'a confirme. Sans cette distinction, un arbre gouverne cesse d''etre gouverne.';

-- 2 ------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.context_review_requests (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id      TEXT REFERENCES app.projects(id) ON DELETE RESTRICT,
    node_type       TEXT NOT NULL CHECK (node_type IN ('topic', 'procedure')),
    node_id         TEXT NOT NULL,
    -- La VERSION est obligatoire : « cette contrainte n'existe plus » ne veut
    -- rien dire si l'on ignore de quelle version on parle.
    node_version    INTEGER NOT NULL CHECK (node_version >= 1),
    note            TEXT NOT NULL CHECK (length(btrim(note)) BETWEEN 1 AND 4000),
    -- Qui, et de quelle nature. Une remarque de machine confondue avec une
    -- remarque humaine vaut moins que rien.
    requested_by    TEXT NOT NULL CHECK (length(btrim(requested_by)) BETWEEN 1 AND 200),
    origin          TEXT NOT NULL DEFAULT 'human' CHECK (origin IN ('human', 'agent')),
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'accepted', 'declined')),
    resolved_by     TEXT,
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((status = 'open') = (resolved_at IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_context_review_requests_open
    ON app.context_review_requests (org_id, project_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_context_review_requests_node
    ON app.context_review_requests (node_type, node_id, node_version);

-- Deux remarques identiques sur la meme version par le meme auteur sont un
-- doublon, pas deux signaux.
CREATE UNIQUE INDEX IF NOT EXISTS uq_context_review_request_open
    ON app.context_review_requests (node_type, node_id, node_version, requested_by, md5(note))
    WHERE status = 'open';

-- 3 ------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.context_candidate_fates (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    candidate_id    TEXT NOT NULL,
    candidate_kind  TEXT NOT NULL CHECK (candidate_kind IN
                        ('topic', 'procedure', 'schema_doc', 'target_field')),
    reason          TEXT NOT NULL CHECK (reason IN
                        ('below_cutoff', 'out_of_scope', 'date_mismatch',
                         'metric_mismatch', 'connector_mismatch')),
    times           INTEGER NOT NULL DEFAULT 1 CHECK (times >= 1),
    last_query      TEXT NOT NULL CHECK (length(btrim(last_query)) BETWEEN 1 AND 400),
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_context_candidate_fate
    ON app.context_candidate_fates (project_id, candidate_id, reason);
CREATE INDEX IF NOT EXISTS idx_context_candidate_fates_recurrent
    ON app.context_candidate_fates (project_id, reason, times DESC);

GRANT SELECT, INSERT, UPDATE ON app.context_review_requests TO connector;
GRANT SELECT, INSERT, UPDATE ON app.context_candidate_fates TO connector;
