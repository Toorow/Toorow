-- infra/nango/migrations/054_render_snapshot_shares.sql
--
-- Story 13.5 volet (b) : partage tokenise des widgets rendus (modalite O1).
--
-- DECISION DE SCHEMA : table DEDIEE app.render_snapshot_shares
--   - Un snapshot peut etre re-partage (historique des partages).
--   - Révocation = revoked_at non-NULL (l'historique est conserve).
--   - Un snapshot supprime entraine la cascade (ON DELETE CASCADE sur snapshot_id).
--   - Plus propre qu'ajouter des colonnes share_token/shared_at sur render_snapshots
--     (separation des responsabilites, un snapshot != un partage).
--
-- DESIGN O1 (modalite ratifiee AD-20) :
--   Un partage = UN GRANT sur UN snapshot FIGE. URL tokenisee read-only publique.
--   Aucun re-run, aucun acces aux donnees live. Le token IS the authorization.
--   Le token est revocable immediatement (revoked_at non-NULL -> 404 sur le endpoint public).
--
-- TABLE : app.render_snapshot_shares
--   - id           : ULID prefixe 'rss_' (PK)
--   - snapshot_id  : FK -> app.render_snapshots (ON DELETE CASCADE)
--   - share_token  : TEXT UNIQUE, URL-safe 192-bit (~32 chars), genere par secrets.token_urlsafe(24)
--   - shared_at    : TIMESTAMPTZ -- horodatage de creation du partage (AD-9 : "partage le <date>")
--   - shared_by    : TEXT -- identite OAuth (sub/client_id) de l'auteur du partage
--   - revoked_at   : TIMESTAMPTZ NULL -- NULL = actif ; non-NULL = revoque
--
-- SCOPING AD-5 :
--   Les endpoints console (create/list/revoke) verifient l'identite sur le projet
--   proprietaire du snapshot (via render_snapshots.project_id).
--   Le endpoint public (GET /api/rendus/shared/{token}) n'a PAS de scope projet :
--   le token IS l'autorisation. Il ne sert que le snapshot fige de CE partage.
--
-- SECURITE O1 :
--   - Le endpoint public ne touche QUE render_snapshot_shares + render_snapshots.
--     Il n'accede JAMAIS aux marts, au projet, aux autres snapshots.
--   - Révocation immediate : tout acces avec revoked_at non-NULL retourne 404.
--   - Isolation : un token ne donne acces qu'a SON snapshot.
--
-- STRICTEMENT ADDITIF : aucune table existante n'est modifiee.
-- IDEMPOTENT : CREATE TABLE IF NOT EXISTS + DROP/ADD CONSTRAINT IF EXISTS.
--

BEGIN;

CREATE TABLE IF NOT EXISTS app.render_snapshot_shares (
    id            TEXT        PRIMARY KEY,
    snapshot_id   TEXT        NOT NULL,
    share_token   TEXT        NOT NULL,
    shared_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    shared_by     TEXT,
    revoked_at    TIMESTAMPTZ
);

-- Unicite du token (le token est l'autorisation -- il doit etre globalement unique).
CREATE UNIQUE INDEX IF NOT EXISTS render_snapshot_shares_token_uniq
    ON app.render_snapshot_shares (share_token);

-- Index pour lister les partages d'un snapshot (actifs ou historique).
CREATE INDEX IF NOT EXISTS render_snapshot_shares_snapshot_id
    ON app.render_snapshot_shares (snapshot_id, shared_at DESC);

-- FK vers render_snapshots : CASCADE (si le snapshot est supprime, ses partages aussi).
ALTER TABLE app.render_snapshot_shares
    DROP CONSTRAINT IF EXISTS fk_rss_snapshot;
ALTER TABLE app.render_snapshot_shares
    ADD CONSTRAINT fk_rss_snapshot
    FOREIGN KEY (snapshot_id) REFERENCES app.render_snapshots(id) ON DELETE CASCADE;

COMMIT;
