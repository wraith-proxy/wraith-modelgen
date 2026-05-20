-- =====================================================================
-- BQ Table DDL: Dynamic, parameterised table creation
-- Ticket:  TJP-20051
--
-- Three patterns — pick one per use case:
--
--   Pattern A: Schema inference from existing source table (zero rows)
--              Copies column types exactly. No STRUCT definition needed.
--
--   Pattern B: CREATE OR REPLACE PROCEDURE — metadata + options dynamic.
--              Schema passed as SQL string fragment via EXECUTE IMMEDIATE.
--
--   Pattern C: Clone schema from another managed table (LIKE).
--              Useful for promoting schema from staging to mart.
--
-- All project/dataset/table identifiers are parameters.
-- No hardcoded strings anywhere in procedure bodies.
-- =====================================================================


-- =====================================================================
-- PATTERN A: Schema inference — copy types from source, load zero rows
-- Use when: source table already has the right types and you want to
--           create a target table with identical structure.
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.utils.create_table_like_source`(
    source_project  STRING,
    source_dataset  STRING,
    source_table    STRING,
    target_project  STRING,
    target_dataset  STRING,
    target_table    STRING,
    partition_col   STRING,   -- column name to partition by (DATE coerced)
    cluster_cols    STRING,   -- comma-separated column names
    tbl_description STRING,
    tbl_labels      STRING    -- BQ label pairs as SQL literal: '("k","v"),("k2","v2")'
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
        SELECT *
        FROM `%s.%s.%s`
        WHERE FALSE
    """,
        target_project, target_dataset, target_table,
        partition_col,
        cluster_cols,
        tbl_description,
        tbl_labels,
        source_project, source_dataset, source_table
    );

    EXECUTE IMMEDIATE ddl;
END;

-- Call example:
-- CALL `project.utils.create_table_like_source`(
--     'src-project',  'raw',      'account_events',
--     'tgt-project',  'staging',  'stg_account_events',
--     'event_ts',
--     'account_id_hashed, event_type',
--     'Staged account events copied from raw',
--     '("owner","data-engineering"),("layer","staging")'
-- );


-- =====================================================================
-- PATTERN B: Explicit schema string passed as parameter
-- Use when: you need full control over types, STRUCT shape, NOT NULL.
--           Pass the column block as a SQL string from your orchestrator
--           (Airflow, Cloud Composer, dbt macro, shell script).
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.utils.create_table_explicit`(
    target_project  STRING,
    target_dataset  STRING,
    target_table    STRING,
    schema_sql      STRING,   -- full column block, e.g. "id STRING NOT NULL, ts TIMESTAMP"
    partition_col   STRING,
    cluster_cols    STRING,
    tbl_description STRING,
    tbl_labels      STRING
)
BEGIN
    DECLARE ddl STRING;

    SET ddl = FORMAT("""
        CREATE OR REPLACE TABLE `%s.%s.%s`
        (
            %s
        )
        PARTITION BY DATE(%s)
        CLUSTER BY %s
        OPTIONS (
            description = %T,
            labels      = [%s]
        )
    """,
        target_project, target_dataset, target_table,
        schema_sql,
        partition_col,
        cluster_cols,
        tbl_description,
        tbl_labels
    );

    EXECUTE IMMEDIATE ddl;
END;

-- Call example — schema_sql carries the full column definition including STRUCTs:
-- CALL `project.utils.create_table_explicit`(
--     'my-project', 'staging', 'stg_account_events',
--     '''
--         event_id          STRING    NOT NULL,
--         event_type        STRING    NOT NULL,
--         account_id_hashed STRING    NOT NULL,
--         event_ts          TIMESTAMP NOT NULL,
--         enrichment        STRUCT<
--             channel       STRING,
--             device        STRUCT<
--                 platform  STRING,
--                 os_version STRING
--             >,
--             flags         ARRAY<STRING>,
--             attributes    ARRAY<STRUCT<key STRING, value STRING>>
--         >,
--         _batch_id         STRING    NOT NULL,
--         _ingested_at      TIMESTAMP NOT NULL
--     ''',
--     'event_ts',
--     'account_id_hashed, event_type',
--     'Staged and typed account events with STRUCT enrichment payload',
--     '("owner","data-engineering"),("layer","staging"),("scd_type","type-2")'
-- );


-- =====================================================================
-- PATTERN C: Clone schema from another managed table (LIKE / AS SELECT)
-- Use when: promoting a model from staging to mart with the same shape.
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.utils.create_table_clone_schema`(
    source_project  STRING,
    source_dataset  STRING,
    source_table    STRING,
    target_project  STRING,
    target_dataset  STRING,
    target_table    STRING,
    partition_col   STRING,
    cluster_cols    STRING,
    tbl_description STRING,
    tbl_labels      STRING
)
BEGIN
    DECLARE ddl STRING;

    -- AS SELECT WHERE FALSE inherits full schema including STRUCT sub-fields,
    -- ARRAY types, NOT NULL constraints, and column ordering.
    SET ddl = FORMAT("""
        CREATE OR REPLACE TABLE `%s.%s.%s`
        PARTITION BY DATE(%s)
        CLUSTER BY %s
        OPTIONS (
            description = %T,
            labels      = [%s]
        )
        AS
        SELECT * EXCEPT (_built_at),
               CAST(NULL AS TIMESTAMP) AS _built_at
        FROM `%s.%s.%s`
        WHERE FALSE
    """,
        target_project, target_dataset, target_table,
        partition_col,
        cluster_cols,
        tbl_description,
        tbl_labels,
        source_project, source_dataset, source_table
    );

    EXECUTE IMMEDIATE ddl;
END;


-- =====================================================================
-- Utility: list all tables in a dataset with their partition/cluster info
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.utils.describe_dataset`(
    target_project  STRING,
    target_dataset  STRING
)
BEGIN
    EXECUTE IMMEDIATE FORMAT("""
        SELECT
            t.table_name,
            t.table_type,
            t.row_count,
            t.size_bytes,
            p.column_name   AS partition_column,
            c.clustering_columns
        FROM `%s.%s.INFORMATION_SCHEMA.TABLES`        AS t
        LEFT JOIN `%s.%s.INFORMATION_SCHEMA.COLUMNS`  AS p
            ON  p.table_name              = t.table_name
            AND p.is_partitioning_column  = 'YES'
        LEFT JOIN (
            SELECT table_name,
                   STRING_AGG(column_name ORDER BY clustering_ordinal_position) AS clustering_columns
            FROM `%s.%s.INFORMATION_SCHEMA.COLUMNS`
            WHERE clustering_ordinal_position IS NOT NULL
            GROUP BY table_name
        ) AS c ON c.table_name = t.table_name
        ORDER BY t.table_name
    """,
        target_project, target_dataset,
        target_project, target_dataset,
        target_project, target_dataset
    );
END;
