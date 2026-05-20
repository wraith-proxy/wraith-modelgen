-- =====================================================================
-- BQ STRUCT Handling, Data Contract & Introspection: Dynamic
-- Ticket:  TJP-20051
--
-- All procedures are table-agnostic. No hardcoded identifiers.
--
-- Procedures:
--   introspect_schema      — full schema including STRUCT sub-fields
--   validate_data_contract — asserts field paths + types match contract
--   detect_schema_drift    — compares today vs yesterday's schema snapshot
--   snapshot_schema        — saves today's schema to baseline table
--
-- STRUCT access patterns at the bottom (query templates, not procedures).
-- =====================================================================


-- =====================================================================
-- PART 1: Schema introspection
-- COLUMN_FIELD_PATHS is the only view that exposes STRUCT sub-fields.
-- INFORMATION_SCHEMA.COLUMNS stops at the top-level STRUCT column.
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.utils.introspect_schema`(
    target_project  STRING,
    target_dataset  STRING,
    target_table    STRING
)
BEGIN
    -- Full schema including all nested STRUCT paths
    EXECUTE IMMEDIATE FORMAT("""
        SELECT
            field_path,
            data_type,
            description,
            collation_name
        FROM `%s.%s.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`
        WHERE table_name = %T
        ORDER BY field_path
    """,
        target_project, target_dataset,
        target_table
    );
END;


-- =====================================================================
-- PART 2: Data contract validation
-- Contract is caller-supplied as ARRAY<STRUCT<field_path, expected_type>>.
-- Returns breach rows. Zero rows = contract satisfied.
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.utils.validate_data_contract`(
    target_project  STRING,
    target_dataset  STRING,
    target_table    STRING,
    contract        ARRAY<STRUCT<field_path STRING, expected_type STRING>>,
    OUT breach_count INT64
)
BEGIN
    DECLARE validation_sql STRING;

    SET validation_sql = FORMAT("""
        WITH contract AS (
            SELECT field_path, expected_type
            FROM UNNEST(@contract)
        ),

        actual AS (
            SELECT
                field_path,
                CASE
                    WHEN data_type LIKE 'ARRAY%%'  THEN 'ARRAY'
                    WHEN data_type LIKE 'STRUCT%%' THEN 'STRUCT'
                    ELSE data_type
                END AS actual_type
            FROM `%s.%s.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`
            WHERE table_name = %T
        )

        SELECT
            c.field_path,
            c.expected_type,
            a.actual_type,
            CASE
                WHEN a.field_path IS NULL          THEN 'MISSING_FIELD'
                WHEN a.actual_type != c.expected_type THEN 'TYPE_MISMATCH'
            END AS breach_reason
        FROM contract AS c
        LEFT JOIN actual AS a USING (field_path)
        WHERE a.field_path IS NULL
           OR a.actual_type != c.expected_type
    """,
        target_project, target_dataset,
        target_table
    );

    EXECUTE IMMEDIATE validation_sql
        USING contract AS contract;

    -- Return breach count for orchestrator to gate on
    EXECUTE IMMEDIATE FORMAT("""
        SELECT COUNT(*)
        FROM (
            WITH contract AS (
                SELECT field_path, expected_type FROM UNNEST(@contract)
            ),
            actual AS (
                SELECT
                    field_path,
                    CASE
                        WHEN data_type LIKE 'ARRAY%%'  THEN 'ARRAY'
                        WHEN data_type LIKE 'STRUCT%%' THEN 'STRUCT'
                        ELSE data_type
                    END AS actual_type
                FROM `%s.%s.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`
                WHERE table_name = %T
            )
            SELECT 1
            FROM contract AS c
            LEFT JOIN actual AS a USING (field_path)
            WHERE a.field_path IS NULL OR a.actual_type != c.expected_type
        )
    """,
        target_project, target_dataset,
        target_table
    )
    USING contract AS contract
    INTO breach_count;

    IF breach_count > 0 THEN
        RAISE USING MESSAGE = FORMAT(
            'Data contract violation: %d breach(es) found in `%s.%s.%s`. '
            || 'Run validate_data_contract to see details.',
            breach_count,
            target_project, target_dataset, target_table
        );
    END IF;
END;

-- Call example:
-- DECLARE v_breaches INT64;
-- CALL `project.utils.validate_data_contract`(
--     'my-project', 'staging', 'stg_account_events',
--     [
--         STRUCT('event_id'                    AS field_path, 'STRING'    AS expected_type),
--         STRUCT('event_ts'                    AS field_path, 'TIMESTAMP' AS expected_type),
--         STRUCT('enrichment'                  AS field_path, 'STRUCT'    AS expected_type),
--         STRUCT('enrichment.channel'          AS field_path, 'STRING'    AS expected_type),
--         STRUCT('enrichment.device'           AS field_path, 'STRUCT'    AS expected_type),
--         STRUCT('enrichment.device.platform'  AS field_path, 'STRING'    AS expected_type),
--         STRUCT('enrichment.flags'            AS field_path, 'ARRAY'     AS expected_type),
--         STRUCT('enrichment.attributes'       AS field_path, 'ARRAY'     AS expected_type),
--         STRUCT('_batch_id'                   AS field_path, 'STRING'    AS expected_type),
--         STRUCT('_ingested_at'                AS field_path, 'TIMESTAMP' AS expected_type)
--     ],
--     v_breaches
-- );


-- =====================================================================
-- PART 3: Schema snapshot — save today's schema to a baseline table
-- Run daily as part of your pipeline. Drift detection reads this.
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.utils.snapshot_schema`(
    target_project      STRING,
    target_dataset      STRING,
    target_table        STRING,
    baseline_project    STRING,
    baseline_dataset    STRING,
    baseline_table      STRING   -- e.g. 'schema_baseline'
)
BEGIN
    EXECUTE IMMEDIATE FORMAT("""
        INSERT INTO `%s.%s.%s`
        (table_name, field_path, data_type, snapshot_date, _ingested_at)
        SELECT
            %T                          AS table_name,
            field_path,
            data_type,
            CURRENT_DATE()              AS snapshot_date,
            CURRENT_TIMESTAMP()         AS _ingested_at
        FROM `%s.%s.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`
        WHERE table_name = %T
    """,
        baseline_project, baseline_dataset, baseline_table,
        target_table,
        target_project, target_dataset,
        target_table
    );
END;


-- =====================================================================
-- PART 4: Schema drift detection
-- Compares today's live schema vs yesterday's snapshot.
-- Returns: ADDED, DROPPED, TYPE_CHANGED rows. Zero rows = clean.
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.utils.detect_schema_drift`(
    target_project      STRING,
    target_dataset      STRING,
    target_table        STRING,
    baseline_project    STRING,
    baseline_dataset    STRING,
    baseline_table      STRING,
    baseline_date       DATE      -- usually DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
)
BEGIN
    EXECUTE IMMEDIATE FORMAT("""
        WITH current_schema AS (
            SELECT field_path, data_type
            FROM `%s.%s.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`
            WHERE table_name = %T
        ),

        baseline_schema AS (
            SELECT field_path, data_type
            FROM `%s.%s.%s`
            WHERE table_name    = %T
              AND snapshot_date = DATE(%T)
        )

        SELECT
            COALESCE(c.field_path, b.field_path)    AS field_path,
            b.data_type                             AS baseline_type,
            c.data_type                             AS current_type,
            CASE
                WHEN b.field_path IS NULL           THEN 'ADDED'
                WHEN c.field_path IS NULL           THEN 'DROPPED'
                WHEN c.data_type != b.data_type     THEN 'TYPE_CHANGED'
            END                                     AS drift_type

        FROM current_schema  AS c
        FULL OUTER JOIN baseline_schema AS b USING (field_path)

        WHERE b.field_path IS NULL
           OR c.field_path IS NULL
           OR c.data_type != b.data_type

        ORDER BY drift_type, field_path
    """,
        target_project, target_dataset,
        target_table,
        baseline_project, baseline_dataset, baseline_table,
        target_table,
        baseline_date
    );
END;


-- =====================================================================
-- PART 5: STRUCT access query templates
-- Not procedures — use these inline in your models/queries.
-- =====================================================================

-- Template: scalar STRUCT field access
-- Replace table, field paths, and filter values.
--
-- SELECT
--     account_id_hashed,
--     enrichment.channel                          AS channel,
--     enrichment.device.platform                  AS device_platform,
--     enrichment.device.os_version                AS os_version
-- FROM `{project}.{dataset}.{table}`
-- WHERE enrichment.channel = '{value}'
-- ;


-- Template: ARRAY<STRING> — UNNEST to rows
--
-- SELECT
--     t.account_id_hashed,
--     flag
-- FROM `{project}.{dataset}.{table}` AS t,
-- UNNEST(t.enrichment.flags) AS flag
-- ;


-- Template: ARRAY<STRUCT> — UNNEST with field access
--
-- SELECT
--     t.account_id_hashed,
--     attr.key,
--     attr.value
-- FROM `{project}.{dataset}.{table}` AS t,
-- UNNEST(t.enrichment.attributes) AS attr
-- WHERE attr.key = '{key_name}'
-- ;


-- Template: pivot ARRAY<STRUCT> key-value to columns
-- Dynamic keys must be known at query time (BQ has no dynamic PIVOT).
--
-- SELECT
--     account_id_hashed,
--     MAX(IF(attr.key = 'risk_tier',      attr.value, NULL)) AS risk_tier,
--     MAX(IF(attr.key = 'kyc_status',     attr.value, NULL)) AS kyc_status,
--     MAX(IF(attr.key = 'onboarding_src', attr.value, NULL)) AS onboarding_src
-- FROM `{project}.{dataset}.{table}` AS t,
-- UNNEST(t.enrichment.attributes) AS attr
-- GROUP BY account_id_hashed
-- ;


-- Template: parse raw JSON STRING → typed STRUCT (raw layer ingestion)
--
-- SELECT
--     event_id,
--     JSON_VALUE(raw_payload, '$.channel')                    AS channel,
--     JSON_VALUE(raw_payload, '$.device.platform')            AS device_platform,
--     STRUCT(
--         JSON_VALUE(raw_payload, '$.channel')                AS channel,
--         STRUCT(
--             JSON_VALUE(raw_payload, '$.device.platform')    AS platform,
--             JSON_VALUE(raw_payload, '$.device.os_version')  AS os_version
--         )                                                   AS device,
--         ARRAY(
--             SELECT AS STRUCT
--                 JSON_VALUE(kv, '$.key')     AS key,
--                 JSON_VALUE(kv, '$.value')   AS value
--             FROM UNNEST(
--                 JSON_EXTRACT_ARRAY(raw_payload, '$.attributes')
--             ) AS kv
--         )                                                   AS attributes
--     )                                                       AS enrichment
-- FROM `{project}.{dataset}.{table}`
-- ;
