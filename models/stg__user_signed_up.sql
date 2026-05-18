/*
  stg__user_signed_up.sql
  Consumption Data Product: raw -> staging

  Stable contract for downstream consumers. Casts and renames are declared in
  the contract; this layer absorbs upstream schema drift so FDPs don't break.
  New source columns flow into raw automatically but are only surfaced here
  once added to the contract.

  Upstream: raw__user_signed_up
  Contract: contracts/example_contract.yml
*/

{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key='event_id',
    schema='staging',
    partition_by={'field': 'received_at', 'data_type': 'timestamp', 'granularity': 'day'},
    cluster_by='user_id'
) }}

with source as (

    select
        EVENT_ID,
        USER_ID,
        SIGNUP_METHOD,
        RECEIVED_AT,
        COUNTRY_CODE
    from {{ ref('raw__user_signed_up') }}

    {%- if is_incremental() %}
    where RECEIVED_AT > (
        select max(received_at)
        from {{ this }}
    )
    {%- endif %}

)

select
    cast(EVENT_ID as STRING) as event_id,
    cast(USER_ID as STRING) as user_id,
    cast(SIGNUP_METHOD as STRING) as signup_method,
    cast(RECEIVED_AT as TIMESTAMP) as received_at,
    cast(COUNTRY_CODE as STRING) as country_code
from source
