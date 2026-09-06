-- Staging Google Business Profile -- reviews (legacy v4 surface).
--
-- THE UPSERT, expressed the only way AD-7 allows. raw_gbp_review is append-only:
-- every pull lands the whole reachable review stream again, because reviews.list
-- has no date filter and an existing review can be EDITED at any time. So the
-- "upsert on reviewId" the story asks for happens here, not at landing: exactly
-- one row survives per (project_id, review_id), the freshest one, and an edited
-- review replaces its earlier text without a single UPDATE against the raw zone.
--
-- Supersede order is review_update_time FIRST, pull_id second. update_time is the
-- provider's own statement of freshness; pull_id only breaks ties between two
-- pulls that saw the same version. Ordering on pull_id alone would work today and
-- would silently pick the wrong row the day two pulls interleave.
--
-- AD-9: a NULL stays NULL. A review without a comment, a reply or a rating is
-- ordinary here -- none of them become an empty string or a 0.
--
-- GRAIN: one row per (project_id, review_id).
{{ config(materialized='view') }}

WITH raw AS (
    SELECT *
    FROM {{ source('raw_gbp', 'raw_gbp_review') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, review_id
        ORDER BY review_update_time DESC, pull_id DESC
    ) = 1
)

SELECT
    raw.review_id,
    raw.location_id,
    raw.account_id,
    -- NON-ADDITIVE: a 1..5 level. Averaged downstream, never summed.
    raw.review_star_rating,
    raw.review_comment,
    raw.review_create_time,
    raw.review_update_time,
    raw.reviewer_display_name,
    raw.reviewer_is_anonymous,
    raw.review_reply_comment,
    raw.review_reply_update_time,
    -- List-level LEVELS of the location, repeated on every review row by the
    -- connector. Read by last-value downstream, never summed.
    raw.average_rating,
    raw.total_review_count,
    -- The review date, as a DATE: the only additive quantity of this profile
    -- (new_reviews) is counted on it. SUBSTR + CAST rather than a date function
    -- so the model reads identically on duckdb and BigQuery.
    CAST(SUBSTR(raw.review_create_time, 1, 10) AS DATE) AS review_date,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
