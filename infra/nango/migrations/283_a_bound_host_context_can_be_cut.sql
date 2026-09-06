-- 283 -- Un contexte de capacites lie peut enfin etre coupe (67-16).
--
-- LE DEFAUT, EN UNE PHRASE. `app.mcp_capability_contexts` (migration 067) n'a
-- qu'un seul ecrivain : l'INSERT de `host_preflight.bind_host_connection`
-- (`host_preflight.py:580`). Aucun UPDATE, aucune revocation, aucune liste,
-- aucun ecran. Une liaison d'hote, une fois ecrite, ne pouvait etre ni retiree
-- ni resserree autrement qu'en SQL a la main. Nomme par
-- `reviews/audit-2026-08-17/10-mcp-app.md:57` et `:84`.
--
-- ET LA LIGNE N'ETAIT LUE PAR PERSONNE. `mcp_profiles.visible_profiles` lisait
-- `enabled_profiles` / `endpoint_binding` / `workspace_evidence_hash` depuis les
-- CLAIMS DU JETON, seulement shape-checkes (commentaire C1,
-- `mcp_profiles.py:738-747`). Une revocation n'aurait donc rien coupe meme si
-- elle avait existe : il n'y avait rien a couper cote lecture. Les deux moities
-- se reparent ensemble ou ne se reparent pas.
--
-- LE MODELE EST DANS LE DEPOT, ET IL VIENT D'ETRE POSE. `session_revocation.py`
-- (67-15d, commit 82726819) revalide l'etat VIVANT a chaque appel plutot qu'a
-- l'emission, exactement comme `render_shares.resolve_session` le fait pour un
-- Share. Meme philosophie ici : le jeton ne porte plus qu'un POINTEUR vers la
-- ligne, les grants sont relus a chaque appel, et une ligne revoquee rend
-- l'appel SUIVANT insights-only. Pas a l'expiration : au prochain appel.
--
-- CE QUE CETTE MIGRATION AJOUTE :
--
--   revoked_at / revoked_by / revocation_reason
--       La coupure. Non NULL => la ligne ne fait plus foi. La ligne n'est
--       jamais supprimee : un contexte qui a existe reste lisible avec sa date
--       de coupure, sinon l'ecran hotes ne pourrait pas dire ce qui a ete
--       coupe, ni quand, ni par qui.
--
--   interactive_presence_evidence_hash
--       `mcp_profiles.interactive_presence_verified` (`:754-769`) exige ce hash
--       pour rendre un `confirmed_write` visible, et AUCUNE colonne ne le
--       portait : il ne pouvait venir que des claims. En le posant ici, la
--       preuve de presence interactive passe par la meme attestation serveur
--       que les trois autres grants -- sinon un tiers de la garde restait sur
--       parole.
--
-- L'IMMUABILITE EST PRESERVEE ET ETENDUE. Le trigger de 067 interdit de muter
-- une liaison approuvee en place (AC3). Il est reecrit ici pour couvrir aussi
-- la nouvelle colonne de preuve, et pour interdire de DE-revoquer : une coupure
-- ne se reprend pas, on relie un nouveau contexte.
--
-- L'INDEX PARTIEL sert le chemin chaud : l'attestation cherche la ligne VIVANTE
-- d'un endpoint a chaque appel MCP.

ALTER TABLE app.mcp_capability_contexts
    ADD COLUMN IF NOT EXISTS revoked_at        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS revoked_by        TEXT,
    ADD COLUMN IF NOT EXISTS revocation_reason TEXT,
    ADD COLUMN IF NOT EXISTS interactive_presence_evidence_hash TEXT;

ALTER TABLE app.mcp_capability_contexts
    DROP CONSTRAINT IF EXISTS mcp_capability_contexts_presence_hex;
ALTER TABLE app.mcp_capability_contexts
    ADD CONSTRAINT mcp_capability_contexts_presence_hex CHECK (
        interactive_presence_evidence_hash IS NULL
        OR interactive_presence_evidence_hash ~ '^[0-9a-f]{64}$'
    );

ALTER TABLE app.mcp_capability_contexts
    DROP CONSTRAINT IF EXISTS mcp_capability_contexts_revocation_complete;
ALTER TABLE app.mcp_capability_contexts
    ADD CONSTRAINT mcp_capability_contexts_revocation_complete CHECK (
        (revoked_at IS NULL AND revoked_by IS NULL)
        OR (revoked_at IS NOT NULL AND revoked_by IS NOT NULL)
    );

-- Le chemin chaud de l'attestation : la ligne VIVANTE d'un endpoint.
CREATE INDEX IF NOT EXISTS mcp_capability_contexts_live_endpoint
    ON app.mcp_capability_contexts (endpoint_binding)
    WHERE revoked_at IS NULL;

-- La liste de l'ecran hotes : les contextes d'une organisation, coupes compris.
CREATE INDEX IF NOT EXISTS mcp_capability_contexts_org_created
    ON app.mcp_capability_contexts (org_id, created_at DESC);

-- Le trigger de 067, reecrit : meme interdit, deux colonnes de plus, et la
-- de-revocation refusee. `CREATE OR REPLACE FUNCTION` remplace le corps ; la
-- migration 067 n'est pas re-editee.
CREATE OR REPLACE FUNCTION app.protect_mcp_capability_context()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NULL THEN
        RAISE EXCEPTION 'a revoked mcp capability context cannot be un-revoked';
    END IF;
    IF OLD.immutable
       AND (
           NEW.org_id IS DISTINCT FROM OLD.org_id
           OR NEW.endpoint_binding IS DISTINCT FROM OLD.endpoint_binding
           OR NEW.enabled_profiles IS DISTINCT FROM OLD.enabled_profiles
           OR NEW.workspace_evidence_hash IS DISTINCT FROM OLD.workspace_evidence_hash
           OR NEW.interactive_presence_evidence_hash
              IS DISTINCT FROM OLD.interactive_presence_evidence_hash
           OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
           OR NEW.catalog_version IS DISTINCT FROM OLD.catalog_version
       ) THEN
        RAISE EXCEPTION 'mcp capability context binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;
