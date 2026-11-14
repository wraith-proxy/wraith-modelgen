*[ODCS]: Open Data Contract Standard: the YAML format every modelgen contract is written in
*[event contract]: A contract for a table the warehouse already receives. modelgen generates its raw and staging models
*[event contracts]: Contracts for tables the warehouse already receives. modelgen generates their raw and staging models
*[Event contract]: A contract for a table the warehouse already receives. modelgen generates its raw and staging models
*[Event contracts]: Contracts for tables the warehouse already receives. modelgen generates their raw and staging models
*[produced contract]: A contract for a table the dbt project builds. modelgen generates the models yml and the tests mapped from its quality rules
*[produced contracts]: Contracts for tables the dbt project builds. modelgen generates the models yml and the tests mapped from their quality rules
*[Produced contract]: A contract for a table the dbt project builds. modelgen generates the models yml and the tests mapped from its quality rules
*[Produced contracts]: Contracts for tables the dbt project builds. modelgen generates the models yml and the tests mapped from their quality rules
*[extract contract]: A contract for a table the dbt project reads but does not build, such as a Parquet extract. modelgen generates the sources yml
*[extract contracts]: Contracts for tables the dbt project reads but does not build, such as a Parquet extract. modelgen generates the sources yml
*[Extract contract]: A contract for a table the dbt project reads but does not build, such as a Parquet extract. modelgen generates the sources yml
*[Extract contracts]: Contracts for tables the dbt project reads but does not build, such as a Parquet extract. modelgen generates the sources yml
*[contract-only introspection]: Taking a source table's columns from the contract's declarations instead of querying the warehouse. Forced with --from-contract; automatic when no BigQuery credentials exist
*[Contract-only introspection]: Taking a source table's columns from the contract's declarations instead of querying the warehouse. Forced with --from-contract; automatic when no BigQuery credentials exist
*[SCD2]: Slowly changing dimension, type 2: one row per version of a record, with validity dates
*[SCD]: Slowly changing dimension
*[CMEK]: Customer-managed encryption key: encrypts BigQuery storage at rest. Not an erasure mechanism
*[KEK]: Key encryption key: held in Cloud KMS, wraps each subject's DEKs
*[DEK]: Data encryption key: the per-subject keyset that encrypts the actual values
*[DEKs]: Data encryption keys: the per-subject keysets that encrypt the actual values
*[KMS]: Key Management Service (Cloud KMS)
*[AEAD]: Authenticated encryption with associated data
*[PII]: Personally identifiable information
*[GDPR]: General Data Protection Regulation
*[IAM]: Identity and Access Management
*[OLAP]: Online analytical processing: large scans and aggregates, the workload BigQuery is built for
*[OLTP]: Online transaction processing: many small row-level reads and writes
*[DDL]: Data definition language: CREATE, ALTER, DROP
*[DML]: Data manipulation language: INSERT, UPDATE, DELETE, MERGE
*[CTE]: Common table expression: a WITH clause
