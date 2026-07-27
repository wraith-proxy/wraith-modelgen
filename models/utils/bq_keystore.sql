-- =====================================================================
-- BQ Keystore: per-subject DEKs for crypto-shred erasure (RTBF)
-- Ticket:  TJP-27072
--
-- One row. One delete. Once.
--
-- The keystore is the only mutable table in an otherwise immutable
-- warehouse. It maps a subject to that subject's wrapped data encryption
-- keys. Every other table holds ciphertext. Deleting one row destroys
-- every ciphertext under those keys, in every table, without rewriting a
-- single partition.
--
-- Key hierarchy (NIST SP 800-57 Pt.1 terminology):
--
--   CMEK  — Cloud KMS key encrypting BigQuery storage at rest. Whole-table
--           blast radius. NOT the erasure mechanism; it protects the media,
--           not the subject.
--   KEK   — Cloud KMS key that wraps each subject's DEKs. Never leaves KMS,
--           never materialises in a query result.
--   DEK   — per-subject Tink keyset. Stored ONLY in wrapped form, here.
--           Unwrapped at query time by KEYS.KEYSET_CHAIN. Deleting the row
--           destroys it.
--
-- Two DEKs per subject, because Tink keysets are primitive-specific and
-- one keyset cannot serve both functions:
--
--   deterministic (AES-SIV)  — the subject-scoped join key. Stable per
--                              keyset, so joins, row counts and aggregates
--                              all survive the shred.
--   randomised (AES-GCM)     — payload. No stable ciphertext to correlate.
--
-- Procedures:
--   issue_keyset   — idempotent. Mints both DEKs for a subject.
--   shred_subject  — the one delete. Idempotent; a second call is a no-op.
--   verify_shred   — asserts the row is gone.
--
-- Encrypt/decrypt query templates at the bottom (templates, not procedures).
--
-- See docs/bigquery/keystore.md for the erasure timeline and citations.
-- =====================================================================


-- =====================================================================
-- The keystore
--
-- Its own dataset, deliberately. Time travel is a DATASET property, so the
-- keystore needs one of its own to run a short window without crippling
-- recovery for the rest of `utils`. Terraform owns it, per the standing
-- decision that Terraform owns datasets:
--
--   ALTER SCHEMA `project.keystore`
--   SET OPTIONS (max_time_travel_hours = 48);   -- the BQ minimum
--
-- Not partitioned: this is a point-lookup table. Clustered on subject_id.
--
-- NEVER snapshot this table. A snapshot is a second copy of the thing whose
-- deletion is the erasure. That includes the modelgen `ds` staging default —
-- the keystore is not a modelgen model and must never become one.
-- =====================================================================

CREATE TABLE IF NOT EXISTS `project.keystore.subject_keys`
(
    subject_id                 STRING    NOT NULL
        OPTIONS (description = "Plaintext subject identifier. The one place it lives; everywhere else it is ciphertext."),
    wrapped_dek_deterministic  BYTES     NOT NULL
        OPTIONS (description = "DETERMINISTIC_AEAD_AES_SIV_CMAC_256 keyset, wrapped by the KEK. Encrypts the join key."),
    wrapped_dek_randomised     BYTES     NOT NULL
        OPTIONS (description = "AEAD_AES_GCM_256 keyset, wrapped by the KEK. Encrypts payload."),
    kek_resource_name          STRING    NOT NULL
        OPTIONS (description = "Cloud KMS key that wraps both DEKs. Needed to unwrap; recorded so rotation can find them."),
    issued_at                  TIMESTAMP NOT NULL,
    rotated_at                 TIMESTAMP,

    -- Unenforced, but it is the invariant staging depends on: the keystore
    -- join must not fan out a row. issue_keyset is idempotent to keep it.
    PRIMARY KEY (subject_id) NOT ENFORCED
)
CLUSTER BY subject_id
OPTIONS (
    description = "Per-subject wrapped DEKs. Deleting a row crypto-shreds that subject across every table. Restrict IAM accordingly: read access here is read access to every subject identifier in the estate."
);


-- =====================================================================
-- issue_keyset — idempotent. Safe to call on every ingest.
--
-- KEYS.NEW_WRAPPED_KEYSET mints the keyset already wrapped, so the
-- plaintext DEK never exists outside BigQuery's execution.
--
-- Caller needs cloudkms.cryptoKeyVersions.useToEncrypt on the KEK.
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.keystore.issue_keyset`(
    subject   STRING,   -- the plaintext subject identifier
    kek_name  STRING    -- gcp-kms://projects/../locations/../keyRings/../cryptoKeys/..
)
BEGIN
    INSERT INTO `project.keystore.subject_keys` (
        subject_id,
        wrapped_dek_deterministic,
        wrapped_dek_randomised,
        kek_resource_name,
        issued_at
    )
    SELECT
        subject,
        KEYS.NEW_WRAPPED_KEYSET(
            kek_name, 'DETERMINISTIC_AEAD_AES_SIV_CMAC_256'
        ),
        KEYS.NEW_WRAPPED_KEYSET(kek_name, 'AEAD_AES_GCM_256'),
        kek_name,
        CURRENT_TIMESTAMP()
    WHERE NOT EXISTS (
        SELECT 1
        FROM   `project.keystore.subject_keys` existing
        WHERE  existing.subject_id = subject
    );
END;


-- =====================================================================
-- shred_subject — the one delete.
--
-- This is the erasure. Nothing downstream is rewritten, no partition is
-- touched, no mart is refreshed. Every ciphertext written under these DEKs
-- becomes undecryptable the moment the row is gone.
--
-- Idempotent: a second call reports 0 and changes nothing. Returns the
-- outcome so the caller can attach it to the erasure request record —
-- keeping that record is an accountability obligation, and it belongs in
-- the ticketing system, not in a table keyed by the identifier you just
-- promised to erase.
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.keystore.shred_subject`(
    subject STRING
)
BEGIN
    DECLARE keys_destroyed INT64;

    DELETE FROM `project.keystore.subject_keys`
    WHERE subject_id = subject;

    SET keys_destroyed = @@row_count;

    SELECT
        'crypto_shred'      AS action,
        subject             AS subject_id,
        keys_destroyed      AS keys_destroyed,
        CURRENT_TIMESTAMP() AS shredded_at,
        IF(
            keys_destroyed = 0,
            'no keyset found — already shredded, or never issued',
            'keyset destroyed; ciphertext is now unrecoverable once the'
            ' time travel and fail-safe windows elapse'
        )                   AS outcome;
END;


-- =====================================================================
-- verify_shred — asserts the keyset is gone.
--
-- Returns one row. breach = TRUE means the shred did not take.
--
-- This proves the KEY is gone, which is the part you control. It cannot
-- prove the ciphertext is unreadable during the recovery windows — see
-- docs/bigquery/keystore.md for that timeline.
-- =====================================================================

CREATE OR REPLACE PROCEDURE `project.keystore.verify_shred`(
    subject STRING
)
BEGIN
    SELECT
        'keyset_still_present' AS check_name,
        subject                AS subject_id,
        COUNT(*) > 0           AS breach,
        COUNT(*)               AS keysets_found
    FROM `project.keystore.subject_keys`
    WHERE subject_id = subject;
END;


-- =====================================================================
-- Query templates — encrypt on the way into staging
--
-- Staging is where encryption happens: it is already the boundary that
-- owns casts and renames, and raw holds source values as received.
--
-- AAD is bound to the subject, so a ciphertext lifted into another row or
-- another table fails to decrypt rather than silently succeeding.
-- =====================================================================

-- INSERT INTO `project.staging.stg__account_events`
-- SELECT
--     DETERMINISTIC_ENCRYPT(
--         KEYS.KEYSET_CHAIN(k.kek_resource_name, k.wrapped_dek_deterministic),
--         src.account_id,
--         CAST(k.subject_id AS BYTES)          -- AAD
--     )                                        AS account_id,
--     AEAD.ENCRYPT(
--         KEYS.KEYSET_CHAIN(k.kek_resource_name, k.wrapped_dek_randomised),
--         src.email_address,
--         CAST(k.subject_id AS BYTES)          -- AAD
--     )                                        AS email_address,
--     src.event_type,                          -- unclassified, plaintext
--     src.event_ts
-- FROM `project.raw.raw__account_events` AS src
-- JOIN `project.keystore.subject_keys`   AS k
--   ON k.subject_id = src.account_id;


-- =====================================================================
-- Query templates — decrypt at read time, and ONLY at read time
--
-- The hard rule, and it is correctness rather than performance:
-- ciphertext propagates through every materialised layer. Decryption
-- happens in a view, or at query time for an authorised reader. A table
-- holding decrypted PII is a second copy the shred never reaches, so
-- erasure fails silently with nothing to surface it.
--
-- Deterministic ciphertext is what makes lookup possible without
-- decrypting the whole table: encrypt the value you are searching for and
-- match on it.
-- =====================================================================

-- CREATE OR REPLACE VIEW `project.marts.v_account_events_decrypted` AS
-- SELECT
--     DETERMINISTIC_DECRYPT_STRING(
--         KEYS.KEYSET_CHAIN(k.kek_resource_name, k.wrapped_dek_deterministic),
--         s.account_id,
--         CAST(k.subject_id AS BYTES)
--     )                                        AS account_id,
--     AEAD.DECRYPT_STRING(
--         KEYS.KEYSET_CHAIN(k.kek_resource_name, k.wrapped_dek_randomised),
--         s.email_address,
--         CAST(k.subject_id AS BYTES)
--     )                                        AS email_address,
--     s.event_type,
--     s.event_ts
-- FROM `project.staging.stg__account_events` AS s
-- JOIN `project.keystore.subject_keys`       AS k
--   ON s.account_id = DETERMINISTIC_ENCRYPT(
--          KEYS.KEYSET_CHAIN(k.kek_resource_name, k.wrapped_dek_deterministic),
--          k.subject_id,
--          CAST(k.subject_id AS BYTES)
--      );
--
-- A shredded subject's rows drop out of this view because the join finds no
-- keyset. The rows remain in staging, still counted by any aggregate that
-- reads staging directly. That is the design: the historical regulatory
-- position still reconstructs, the subject does not.
