-- The Context Hub reads the governed app.context_topics store exclusively.
BEGIN;

DROP TABLE IF EXISTS app.knowledge_entries;

COMMIT;
