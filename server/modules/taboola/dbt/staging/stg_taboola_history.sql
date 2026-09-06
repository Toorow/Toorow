-- Staging Taboola -- Backstage campaign history (profile `campaign_history`,
-- landing `context_events`).
--
-- AD-7 SUPERSEDE, WHICH THIS MODEL DID NOT HAVE. It was a bare `SELECT *` over
-- an append-only raw table with a `unique` test on `record_id`: the second pull
-- of any overlapping window landed the same immutable record again, the view
-- returned it twice, and the test went red on real data. That is the
-- execution-substrate criterion "a retried unit is not idempotent" -- the whole
-- point of Cloud Tasks retry is that a re-pull LANDS again and exactly one row
-- per business key SURVIVES, and this was the one staging model of 54 that
-- broke the contract.
--
-- GRAIN: one row per (project_id, account_id, record_id). `record_id` is the
-- provider's immutable identity for a change record (manifest: "Immutable
-- history record id."), so it is what a re-pull collides on; `account_id`
-- scopes it to the account that emitted it and `project_id` to the tenant.
-- `change_time`, `change_type` and `entity_id` are properties OF that record,
-- not additional identity: adding them to the partition would let a corrected
-- re-emission of the same record survive twice.
--
-- ORDER BY pull_id DESC alone, no provider freshness column. Unlike
-- stg_gbp_review -- where an edited review carries its own update_time -- a
-- Backstage history record is immutable, so the only freshness statement
-- available is which pull landed it. ULIDs are lexicographically monotonic, so
-- `pull_id DESC` is "most recent pull".
{{ config(materialized='view') }}

WITH raw AS (
    SELECT *
    FROM {{ source('raw_taboola', 'raw_taboola_history') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, account_id, record_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.account_id,
    raw.dimension,
    raw.record_id,
    raw.change_time,
    raw.change_type,
    raw.entity_id,
    -- The whole provider record, kept as evidence by the connector: a field the
    -- module does not model yet is preserved rather than dropped.
    raw.payload_json,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
