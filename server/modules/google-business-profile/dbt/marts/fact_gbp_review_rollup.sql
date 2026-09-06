-- fact_gbp_review_rollup: Google Business Profile reviews, per location per day.
--
-- THE NON-ADDITIVE MART. This is the model the story exists for, so the rule is
-- written where it is enforced rather than only in a document:
--
--   * new_reviews IS additive. It counts reviews created that day -- a flow, and
--     the only quantity here that survives a SUM across days or locations.
--   * new_reviews_avg_star_rating IS NOT. It averages the 1..5 levels of that
--     day's new reviews. Summing it produces a number on no scale at all;
--     averaging the averages across days is wrong too unless weighted by
--     new_reviews, which is why the weight sits in the same row.
--   * location_average_rating / location_total_review_count ARE NOT EITHER, and
--     they are not per-day quantities in the first place: the provider states
--     them as the CURRENT level of the location, once per listing. They are read
--     LAST-VALUE and carried on every row with location_rating_observed_at
--     saying when they were observed -- so nobody mistakes them for "the rating
--     that day", which the source never told us.
--
-- A day with no new review produces NO row (the source says nothing about it);
-- gap-filling to 0 reviews would be a claim, and a rating of 0 would be a value
-- outside the 1..5 scale.
--
-- GRAIN: one row per (project_id, location_id, review_date).
{{ config(materialized='view') }}

WITH reviews AS (
    SELECT * FROM {{ ref('stg_gbp_review') }}
),

-- The location-level LEVELS as of the freshest observation. One row per
-- location, deliberately not per day: the provider publishes a current value,
-- not a history.
location_level AS (
    SELECT
        project_id,
        location_id,
        average_rating AS location_average_rating,
        total_review_count AS location_total_review_count,
        loaded_at AS location_rating_observed_at
    FROM reviews
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, location_id
        ORDER BY loaded_at DESC, pull_id DESC
    ) = 1
),

daily AS (
    SELECT
        project_id,
        location_id,
        review_date,
        -- ADDITIVE: a flow of new reviews.
        COUNT(*) AS new_reviews,
        -- NON-ADDITIVE: average of the levels of that day's new reviews.
        AVG(CAST(review_star_rating AS {{ toorow_float_type() }})) AS new_reviews_avg_star_rating,
        COUNT(review_reply_comment) AS new_reviews_replied,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS loaded_at
    FROM reviews
    WHERE review_date IS NOT NULL
    GROUP BY project_id, location_id, review_date
)

SELECT
    daily.project_id,
    daily.review_date,
    daily.location_id,
    daily.new_reviews,
    daily.new_reviews_avg_star_rating,
    daily.new_reviews_replied,
    location_level.location_average_rating,
    location_level.location_total_review_count,
    location_level.location_rating_observed_at,
    daily.pull_id,
    daily.loaded_at
FROM daily
LEFT JOIN location_level
    ON daily.project_id = location_level.project_id
    AND daily.location_id = location_level.location_id
