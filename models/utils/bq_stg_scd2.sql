-- =====================================================================
-- BQ SCD2: Dynamic, parameterised — works for any table
-- Ticket:  TJP-20051, narrowed by TJP-27071
--
-- SCD2 is the carve-out, not the default. Staging materialises as a
-- datestamped daily snapshot (ds); SCD2 is derived on top of that base
-- when a regulated consumer needs an immutable who-changed-what-when
-- trail. See docs/bigquery/scd2.md.
--
-- Two procedures:
--
--   rebuild_scd2   — full CREATE OR REPLACE from the ds base. Always
--                    correct, and cheap because the base is already
--                    partitioned.
--
--   validate_scd2  — three quality checks on whatever SCD2 output exists.
--
-- Parameters control every identifier and column name.
-- The SELECT projection (select_cols) is caller-supplied SQL — gives
-- full control over casting, STRUCT construction, computed columns.
-- =====================================================================


-- =====================================================================
-- Full rebuild SCD2
-- Drops and recreates target from source on every run.
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.utils.rebuild_scd2`(
    source_project      STRING,
    source_dataset      STRING,
    source_table        STRING,
    target_project      STRING,
    target_dataset      STRING,
    target_table        STRING,
    unique_key          STRING,   -- column to partition SCD2 window on (e.g. 'account_id_hashed')
    event_ts_col        STRING,   -- timestamp column for ordering events
    tiebreak_col        STRING,   -- column for deterministic dedup of simultaneous events (e.g. 'event_type')
    partition_col       STRING,   -- column to partition target table on
    cluster_cols        STRING,   -- comma-separated cluster columns
    select_cols         STRING,   -- projection SQL string — maps source → SCD2 output columns
    tbl_description     STRING,
    tbl_labels          STRING
)
BEGIN
    DECLARE ddl STRING;

    SET ddl = FORMAT("""
        CREATE OR REPLACE TABLE `%s.%s.%s`
        PARTITION BY DATE(%s)
        CLUSTER BY %s
        OPTIONS (
            description = %T,
            labels      = [%s]
        )
        AS

        WITH

        source_events AS (
            SELECT %s
            FROM `%s.%s.%s`
        ),

        scd2 AS (
            SELECT
                *,
                LEAD(%s) OVER (
                    PARTITION BY %s
                    ORDER BY     %s
                )                               AS valid_to,
                LEAD(%s) OVER (
                    PARTITION BY %s
                    ORDER BY     %s
                ) IS NULL                       AS is_current,
                ROW_NUMBER() OVER (
                    PARTITION BY %s
                    ORDER BY     %s
                )                               AS record_version,
                CURRENT_TIMESTAMP()             AS _built_at
            FROM source_events
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY %s, %s
                ORDER BY     %s
            ) = 1
        )

        SELECT * FROM scd2
    """,
        -- CREATE OR REPLACE TABLE target
        target_project, target_dataset, target_table,
        partition_col,
        cluster_cols,
        tbl_description,
        tbl_labels,
        -- source_events CTE
        select_cols,
        source_project, source_dataset, source_table,
        -- LEAD for valid_to (uses event_ts_col as the window bound)
        event_ts_col, unique_key, event_ts_col,
        -- LEAD IS NULL for is_current
        event_ts_col, unique_key, event_ts_col,
        -- ROW_NUMBER for record_version
        unique_key, event_ts_col,
        -- QUALIFY dedup: same entity + same ts, deterministic tiebreak on tiebreak_col
        unique_key, event_ts_col, tiebreak_col
    );

    EXECUTE IMMEDIATE ddl;
END;

-- Call example:
-- CALL `project.utils.rebuild_scd2`(
--     'my-project', 'staging',  'stg_account_events',
--     'my-project', 'marts',    'dim_accounts',
--     'account_id_hashed',
--     'event_ts',
--     'event_type',   -- tiebreak_col: alphabetical on event_type for simultaneous events
--     'valid_from',
--     'account_id_hashed',
--     '''
--         account_id_hashed,
--         user_id_hashed,
--         account_type,
--         CASE
--             WHEN event_type IN (''created'', ''reopened'') THEN ''open''
--             WHEN event_type = ''closed''                   THEN ''closed''
--         END  AS status,
--         event_ts AS valid_from
--     ''',
--     'SCD Type 2 account dimension. One row per account per state period.',
--     '("owner","data-engineering"),("layer","marts"),("scd_type","type-2")'
-- );


-- =====================================================================
-- Validation procedure — table-agnostic SCD2 quality checks
-- Pass any SCD2 target table. Returns breach rows; zero = clean.
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.utils.validate_scd2`(
    target_project  STRING,
    target_dataset  STRING,
    target_table    STRING,
    unique_key      STRING,
    valid_from_col  STRING,
    valid_to_col    STRING
)
BEGIN
    -- Check 1: more than one current row per entity
    EXECUTE IMMEDIATE FORMAT("""
        SELECT
            'multiple_current_rows'     AS check_name,
            %s                          AS entity_key,
            COUNT(*)                    AS current_row_count
        FROM `%s.%s.%s`
        WHERE is_current = TRUE
        GROUP BY %s
        HAVING COUNT(*) > 1
    """,
        unique_key,
        target_project, target_dataset, target_table,
        unique_key
    );

    -- Check 2: gaps between consecutive periods
    -- valid_to of row N should equal valid_from of row N+1
    EXECUTE IMMEDIATE FORMAT("""
        SELECT
            'period_gap'                AS check_name,
            %s                          AS entity_key,
            %s                          AS gap_starts_after,
            LEAD(%s) OVER (
                PARTITION BY %s ORDER BY %s
            )                           AS next_valid_from
        FROM `%s.%s.%s`
        QUALIFY
            %s IS NOT NULL
            AND %s != LEAD(%s) OVER (
                PARTITION BY %s ORDER BY %s
            )
    """,
        unique_key,
        valid_to_col,
        valid_from_col,
        unique_key, valid_from_col,
        target_project, target_dataset, target_table,
        valid_to_col,
        valid_to_col, valid_from_col,
        unique_key, valid_from_col
    );

    -- Check 3: overlapping periods
    EXECUTE IMMEDIATE FORMAT("""
        SELECT
            'overlapping_periods'       AS check_name,
            a.%s                        AS entity_key,
            a.%s                        AS period_a_from,
            a.%s                        AS period_a_to,
            b.%s                        AS period_b_from
        FROM `%s.%s.%s` AS a
        JOIN `%s.%s.%s` AS b
            ON  a.%s = b.%s
            AND a.%s < b.%s
            AND (a.%s IS NULL OR a.%s > b.%s)
    """,
        unique_key,
        valid_from_col,
        valid_to_col,
        valid_from_col,
        target_project, target_dataset, target_table,
        target_project, target_dataset, target_table,
        unique_key, unique_key,
        valid_from_col, valid_from_col,
        valid_to_col, valid_to_col, valid_from_col
    );
END;
