-- 214_a_receipt_remembers_who_sent_it.sql
--
-- UN RECU DIT A QUI LA LIVRAISON ETAIT ADRESSEE, ET PAS DE QUI ELLE VENAIT.
-- `app.inbound_receipts` porte `recipient_hash` depuis la migration 094 et rien
-- sur l'expediteur. Tant que rien ne lisait l'expediteur, l'absence ne coutait
-- rien. La story 57.3 lui donne un lecteur : un Datastream peut declarer une
-- liste blanche d'expediteurs, et cette liste doit mordre sur LES DEUX chemins
-- d'import -- l'arrivee directe (`core.inbound_processing`) et le rejeu d'une
-- livraison conservee (`core.inbound_reprocess`).
--
-- Le rejeu ne voit pas le manifeste : il repart des octets conserves et du recu.
-- Sans ces deux colonnes, il n'existe aucune preuve durable de l'expediteur au
-- moment ou il faut decider -- et une liste blanche avec un chemin qui ne la lit
-- pas n'est pas une liste blanche. Le controle est pose la ou les deux chemins
-- se rejoignent (`inbound_ingest.ingest_inbound_file`, a cote de la liste
-- blanche de CANAUX qui y vit deja) ; ces colonnes sont ce qu'il y lit.
--
-- JAMAIS L'ADRESSE EN CLAIR. Meme regle que `recipient_hash`, meme contrainte,
-- et pour la meme raison : le recu est conserve, l'adresse d'un expediteur est
-- une donnee personnelle, et un hachage suffit a decider. Deux hachages plutot
-- qu'un pour qu'un domaine entier puisse etre autorise sans qu'aucune adresse
-- ne soit ecrite nulle part.
--
-- ADDITIVE ET NULLABLE. Tout recu ecrit avant cette migration garde NULL, ce qui
-- se lit « expediteur inconnu » -- et un expediteur inconnu face a une liste
-- declaree est refuse (fail-closed), jamais accepte par defaut.

ALTER TABLE app.inbound_receipts
    ADD COLUMN IF NOT EXISTS sender_hash TEXT,
    ADD COLUMN IF NOT EXISTS sender_domain_hash TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_inbound_receipts_sender_hash_shape'
    ) THEN
        ALTER TABLE app.inbound_receipts
            ADD CONSTRAINT ck_inbound_receipts_sender_hash_shape
            CHECK (
                sender_hash IS NULL
                OR (LENGTH(sender_hash) = 64 AND sender_hash ~ '^[0-9a-f]{64}$')
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_inbound_receipts_sender_domain_hash_shape'
    ) THEN
        ALTER TABLE app.inbound_receipts
            ADD CONSTRAINT ck_inbound_receipts_sender_domain_hash_shape
            CHECK (
                sender_domain_hash IS NULL
                OR (
                    LENGTH(sender_domain_hash) = 64
                    AND sender_domain_hash ~ '^[0-9a-f]{64}$'
                )
            );
    END IF;
END
$$;

COMMENT ON COLUMN app.inbound_receipts.sender_hash IS
    'sha256 de l''adresse d''expediteur normalisee (minuscules, sans espaces). '
    'Jamais l''adresse. Lu par la liste blanche declaree du Datastream, sur les '
    'deux chemins d''import. NULL = expediteur inconnu, refuse face a une liste.';

COMMENT ON COLUMN app.inbound_receipts.sender_domain_hash IS
    'sha256 du domaine de l''expediteur. Permet d''autoriser un domaine entier '
    'sans qu''aucune adresse ne soit ecrite.';
