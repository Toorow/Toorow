-- Full-grain monday state. Raw batches remain append-only; newest pull wins.
{{ config(materialized='view') }}

select
    cast(board_id as {{ dbt.type_string() }}) as board_id,
    cast(item_id as {{ dbt.type_string() }}) as item_id,
    cast(item_name as {{ dbt.type_string() }}) as item_name,
    nullif(cast(group_id as {{ dbt.type_string() }}), '') as group_id,
    nullif(cast(parent_item_id as {{ dbt.type_string() }}), '') as parent_item_id,
    cast(updated_at as timestamp) as updated_at,
    cast(column_id as {{ dbt.type_string() }}) as column_id,
    cast(column_type as {{ dbt.type_string() }}) as column_type,
    column_text,
    column_raw_value,
    known_type,
    schema_fingerprint,
    pull_id,
    loaded_at,
    project_id
from {{ source('raw_monday', 'raw_monday_board_snapshot') }}
qualify row_number() over (
    partition by project_id, board_id, item_id, column_id
    order by pull_id desc
) = 1