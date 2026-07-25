{{ config(materialized='view') }}
SELECT * FROM {{ source('raw_taboola', 'raw_taboola_history') }}
