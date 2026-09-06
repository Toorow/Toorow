-- Une remarque en file peut PORTER le changement qu'elle propose.
--
-- POURQUOI PAS LE RAIL DE PUBLICATION GOUVERNEE, et c'est un choix, pas un
-- raccourci. `governed_publication` fait porter a une confirmation un secret
-- OPAQUE, cache du modele, avec protection de rejeu et idempotence -- parce
-- qu'une publication de mapping AVANCE UN POINTEUR : elle change ce que les
-- donnees VEULENT DIRE, et un retour arriere demande une operation dediee.
--
-- Un lien de taxonomie n'a rien de tout cela. Il est reversible par un appel
-- (`DELETE /api/context/business-links/{id}`), il ne deplace aucun pointeur, et
-- il ne change la lecture d'aucune donnee. Lui imposer la meme ceremonie serait
-- recopier le mecanisme le plus lourd du depot parce qu'il existe -- et une
-- ceremonie qu'on ne peut pas justifier finit contournee.
--
-- CE QUI RESTE VRAI, ET QUI EST L'ESSENTIEL : un humain accepte. La file
-- `context_review_requests` porte deja `origin` (`human` / `agent`) et
-- `status` (`open` / `accepted` / `declined`) depuis la migration 202. Il ne
-- manquait que la CHARGE : ce que la remarque propose de faire, en plus de le
-- dire en prose.
--
-- Une remarque sans charge reste une remarque -- c'est le cas courant, et rien
-- ne change pour elle.

ALTER TABLE app.context_review_requests
    ADD COLUMN IF NOT EXISTS proposed_change JSONB;

-- Une charge doit etre un objet nomme, jamais une liste ni un scalaire : c'est
-- ce qui permet d'y lire un `kind` et de refuser ce qu'on ne sait pas appliquer.
ALTER TABLE app.context_review_requests
    DROP CONSTRAINT IF EXISTS ck_context_review_proposed_change;
ALTER TABLE app.context_review_requests
    ADD CONSTRAINT ck_context_review_proposed_change
    CHECK (proposed_change IS NULL OR jsonb_typeof(proposed_change) = 'object');

-- Ce qu'une acceptation a REELLEMENT produit. Sans cette colonne, une remarque
-- acceptee et une remarque appliquee se ressemblent -- et l'on ne saurait pas
-- distinguer << un humain a dit oui >> de << le changement est en base >>.
ALTER TABLE app.context_review_requests
    ADD COLUMN IF NOT EXISTS applied_ref TEXT;

COMMENT ON COLUMN app.context_review_requests.proposed_change IS
    'Ce que la remarque propose de faire, ou NULL si elle ne fait que le dire. '
    'Porte un `kind` que le code sait appliquer, et rien d''autre.';
COMMENT ON COLUMN app.context_review_requests.applied_ref IS
    'L''identifiant de ce que l''acceptation a produit. NULL tant que rien n''a '
    'ete applique -- accepter et appliquer ne sont pas le meme fait.';

CREATE INDEX IF NOT EXISTS idx_context_review_requests_proposals
    ON app.context_review_requests (org_id, status)
    WHERE proposed_change IS NOT NULL;
