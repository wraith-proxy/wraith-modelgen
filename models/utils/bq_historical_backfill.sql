-- =====================================================================
-- BQ Historical Backfill: Dynamic, parameterised
-- Ticket:  TJP-20051
--
-- Procedure handles any source→target table pair.
-- No hardcoded project, dataset, table, date, or batch ID.
--
-- Caller supplies:
--   - source and target identifiers
--   - the SELECT column list (as SQL string) — gives full control
--     over casting, aliasing, STRUCT construction
--   - cutoff date — records BEFORE this date are backfill territory
--   - batch_id — traceable run identifier for audit
--
-- Pattern:
--   1. bq_table_ddl.sql        → CREATE tables (schema only)
--   2. THIS FILE               → load all history up to cutoff_date
--   3. Daily incremental       → owns everything from cutoff_date onward
-- =====================================================================


-- =====================================================================
-- Core backfill procedure
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.utils.historical_backfill`(
    source_project  STRING,
    source_dataset  STRING,
    source_table    STRING,
    target_project  STRING,
    target_dataset  STRING,
    target_table    STRING,
    select_cols     STRING,   -- SELECT column list as SQL string
    ts_column       STRING,   -- timestamp/date column used for cutoff filter
    cutoff_date     DATE,     -- backfill owns records BEFORE this date
    batch_id        STRING,   -- e.g. 'BACKFILL-2026-05-20' — for audit trail
    OUT rows_loaded INT64
)
BEGIN
    DECLARE dml STRING;

    -- Safety: abort if target already has rows from this batch_id.
    -- Prevents double-load on reruns without cleanup.
    EXECUTE IMMEDIATE FORMAT("""
        SELECT COUNT(*)
        FROM `%s.%s.%s`
        WHERE _batch_id = %T
    """,
        target_project, target_dataset, target_table,
        batch_id
    ) INTO rows_loaded;

    IF rows_loaded > 0 THEN
        RAISE USING MESSAGE = FORMAT(
            'Backfill aborted: batch_id %T already present in `%s.%s.%s` (%d rows). '
            || 'Delete existing rows or use a new batch_id.',
            batch_id,
            target_project, target_dataset, target_table,
            rows_loaded
        );
    END IF;

    -- Main INSERT
    SET dml = FORMAT("""
        INSERT INTO `%s.%s.%s`
        SELECT
            %s
        FROM `%s.%s.%s`
        WHERE DATE(%s) < DATE(%T)
    """,
        target_project, target_dataset, target_table,
        select_cols,
        source_project, source_dataset, source_table,
        ts_column,
        cutoff_date
    );

    EXECUTE IMMEDIATE dml;

    -- Return row count loaded
    EXECUTE IMMEDIATE FORMAT("""
        SELECT COUNT(*)
        FROM `%s.%s.%s`
        WHERE _batch_id = %T
    """,
        target_project, target_dataset, target_table,
        batch_id
    ) INTO rows_loaded;

END;


-- =====================================================================
-- Verification procedure
-- Run after backfill to confirm load quality.
-- Returns: row counts by partition date, min/max ts, batch coverage.
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.utils.verify_backfill`(
    target_project  STRING,
    target_dataset  STRING,
    target_table    STRING,
    batch_id        STRING,
    ts_column       STRING
)
BEGIN
    -- Row counts by day
    EXECUTE IMMEDIATE FORMAT("""
        SELECT
            DATE(%s)                    AS partition_date,
            COUNT(*)                    AS row_count,
            MIN(%s)                     AS earliest_ts,
            MAX(%s)                     AS latest_ts
        FROM `%s.%s.%s`
        WHERE _batch_id = %T
        GROUP BY partition_date
        ORDER BY partition_date
    """,
        ts_column,
        ts_column,
        ts_column,
        target_project, target_dataset, target_table,
        batch_id
    );

    -- Overlap check: any rows outside backfill territory
    -- (should be zero — if not, cutoff_date logic has a bug)
    EXECUTE IMMEDIATE FORMAT("""
        SELECT
            'overlap_check'             AS check_name,
            COUNT(*)                    AS rows_in_incremental_window
        FROM `%s.%s.%s`
        WHERE _batch_id = %T
          AND DATE(%s) >= (
              SELECT MAX(DATE(%s))
              FROM `%s.%s.%s`
              WHERE _batch_id != %T
          )
    """,
        target_project, target_dataset, target_table,
        batch_id,
        ts_column,
        ts_column,
        target_project, target_dataset, target_table,
        batch_id
    );
END;


-- =====================================================================
-- Orchestration script — calls procedures in sequence
-- Replace parameter values with your actual identifiers.
-- =====================================================================

-- Declare run-level variables once at the top of your script.
DECLARE v_source_project  STRING DEFAULT 'src-project';
DECLARE v_source_dataset  STRING DEFAULT 'monzo_datawarehouse';
DECLARE v_target_project  STRING DEFAULT 'tgt-project';
DECLARE v_target_dataset  STRING DEFAULT 'raw';
DECLARE v_cutoff_date     DATE   DEFAULT DATE('2026-01-01');
DECLARE v_batch_id        STRING DEFAULT CONCAT('BACKFILL-', CAST(CURRENT_DATE() AS STRING));
DECLARE v_rows_loaded     INT64;

-- Backfill account_created events
CALL `project.utils.historical_backfill`(
    v_source_project,  v_source_dataset, 'account_created',
    v_target_project,  v_target_dataset, 'account_events',
    -- select_cols: maps source columns → target schema, constructs _batch_id/_ingested_at inline
    """
        GENERATE_UUID()                         AS event_id,
        'created'                               AS event_type,
        account_id_hashed,
        user_id_hashed,
        account_type,
        created_ts                              AS event_ts,
        TO_JSON_STRING(STRUCT(
            account_id_hashed,
            user_id_hashed,
            account_type,
            created_ts
        ))                                      AS raw_payload,
        v_batch_id                              AS _batch_id,
        CURRENT_TIMESTAMP()                     AS _ingested_at
    """,
    'created_ts',
    v_cutoff_date,
    v_batch_id,
    v_rows_loaded
);
SELECT FORMAT('account_created backfill: %d rows loaded', v_rows_loaded) AS status;

-- Backfill account_closed events
CALL `project.utils.historical_backfill`(
    v_source_project,  v_source_dataset, 'account_closed',
    v_target_project,  v_target_dataset, 'account_events',
    """
        GENERATE_UUID()                         AS event_id,
        'closed'                                AS event_type,
        account_id_hashed,
        NULL                                    AS user_id_hashed,
        NULL                                    AS account_type,
        closed_ts                               AS event_ts,
        TO_JSON_STRING(STRUCT(account_id_hashed, closed_ts)) AS raw_payload,
        v_batch_id                              AS _batch_id,
        CURRENT_TIMESTAMP()                     AS _ingested_at
    """,
    'closed_ts',
    v_cutoff_date,
    v_batch_id,
    v_rows_loaded
);
SELECT FORMAT('account_closed backfill: %d rows loaded', v_rows_loaded) AS status;

-- Backfill account_reopened events
CALL `project.utils.historical_backfill`(
    v_source_project,  v_source_dataset, 'account_reopened',
    v_target_project,  v_target_dataset, 'account_events',
    """
        GENERATE_UUID()                         AS event_id,
        'reopened'                              AS event_type,
        account_id_hashed,
        NULL                                    AS user_id_hashed,
        NULL                                    AS account_type,
        reopened_ts                             AS event_ts,
        TO_JSON_STRING(STRUCT(account_id_hashed, reopened_ts)) AS raw_payload,
        v_batch_id                              AS _batch_id,
        CURRENT_TIMESTAMP()                     AS _ingested_at
    """,
    'reopened_ts',
    v_cutoff_date,
    v_batch_id,
    v_rows_loaded
);
SELECT FORMAT('account_reopened backfill: %d rows loaded', v_rows_loaded) AS status;

-- Verify all three loads
CALL `project.utils.verify_backfill`(
    v_target_project, v_target_dataset, 'account_events',
    v_batch_id, 'event_ts'
);
