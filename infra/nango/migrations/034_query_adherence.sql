-- Story 11.6: measured pre-query gate adherence (AD-18).
--
-- The pre-query gate is MEASURED, never enforced. For each data query
-- (get_daily_report / get_report / get_card) we record whether a context call
-- (search_context / get_procedure) preceded it within the SAME SESSION. The
-- primary sink is a Langfuse trace attribute (best-effort); this table is the
-- degrade-clean Postgres FALLBACK so adherence stays measurable even when
-- Langfuse is not up (AI-34 partial) -- see server/core/adherence.py.
--
-- "Same session" key (documented in adherence.py): there is no reliable
-- conversation identity across MCP calls, so the session key = the trace-parent
-- id (W3C traceparent trace_id when tracing is on) ELSE a per-process time
-- window bucket. The column below stores whichever key was in force so a later
-- analysis (Epic 14 judges; 11.6 only MEASURES) can group by it.
--
-- Additive after migrations 001-033. Never blocking: a failed insert here must
-- never fail a data query (adherence.py swallows write errors).
--
-- RETENTION (TODO / deferred): this is an append-only MEASUREMENT log with no
-- built-in pruning, so it grows unbounded. Rows older than ~90 days carry no value
-- (Epic 14 slices recent adherence rate). A nightly prune
--   DELETE FROM app.query_adherence WHERE created_at < now() - INTERVAL '90 days';
-- is DEFERRED here on purpose: single-tenant / P3-dev volume is negligible and no
-- scheduler step is wired in this pass. Documented so the unbounded growth is a
-- KNOWN, tracked follow-up (add to the nightly maintenance job in Phase B) rather
-- than a silent hole. idx_query_adherence_created above already supports the prune.

BEGIN;

CREATE TABLE IF NOT EXISTS app.query_adherence (
    id            TEXT        NOT NULL PRIMARY KEY,          -- adh_<ULID>
    project_id    TEXT        NULL,                          -- resolved project (AD-5); NULL tolerated
    session_key   TEXT        NOT NULL,                      -- trace parent id OR time-window bucket
    session_kind  TEXT        NOT NULL                       -- how session_key was derived
                              CHECK (session_kind IN ('trace', 'time_window')),
    data_tool     TEXT        NOT NULL,                      -- get_daily_report | get_report | get_card
    adherent      BOOLEAN     NOT NULL,                      -- did a context call precede this query?
    context_tool  TEXT        NULL,                          -- last context tool seen (search_context/get_procedure) or NULL
    trace_id      TEXT        NULL,                          -- 32-hex OTel trace_id when tracing is on (else NULL)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Adherence RATE queries group by session and/or tool over a time range.
CREATE INDEX IF NOT EXISTS idx_query_adherence_session
    ON app.query_adherence (session_key);
CREATE INDEX IF NOT EXISTS idx_query_adherence_created
    ON app.query_adherence (created_at);
CREATE INDEX IF NOT EXISTS idx_query_adherence_project
    ON app.query_adherence (project_id);

COMMIT;
