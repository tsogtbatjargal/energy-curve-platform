-- ADR-0023: the tables of the Redshift warehouse. Everything lives in `public`, every table is
-- named ecp_*, and the role that loads them creates them (a new schema needs a privilege the
-- namespace creator may not have; every user may create tables in public). Shared with the local
-- Postgres, which runs this same file in the tests. Idempotent.
--
-- The column order of each table is the column order of the file it is loaded from, because COPY
-- maps by position: ecp_shape_params is the Glue job's CSV (position,month,s,n_obs); the other
-- three are the published gold parquet files (GOLD_SCHEMA, REVISION_SCHEMA, CURVE_SCHEMA).
-- Redshift records primary keys but does not enforce them, so none is declared here.

CREATE TABLE IF NOT EXISTS public.ecp_shape_params (
    "position" varchar(8)    NOT NULL,
    "month"    smallint      NOT NULL,
    s          numeric(20,10) NOT NULL,
    n_obs      integer       NOT NULL
);

CREATE TABLE IF NOT EXISTS public.ecp_observations (
    source            varchar(32)   NOT NULL,
    series_id         varchar(32)   NOT NULL,
    observation_date  date          NOT NULL,
    price             numeric(18,6) NOT NULL,
    unit              varchar(32)   NOT NULL,
    non_positive      boolean       NOT NULL,
    retrieved_at      timestamptz   NOT NULL,
    logical_input_id  varchar(256)  NOT NULL,
    raw_artifact_key  varchar(1024) NOT NULL,
    last_seen_at      timestamptz   NOT NULL
);

CREATE TABLE IF NOT EXISTS public.ecp_revisions (
    source                          varchar(32)   NOT NULL,
    series_id                       varchar(32)   NOT NULL,
    observation_date                date          NOT NULL,
    price                           numeric(18,6) NOT NULL,
    unit                            varchar(32)   NOT NULL,
    non_positive                    boolean       NOT NULL,
    retrieved_at                    timestamptz   NOT NULL,
    logical_input_id                varchar(256)  NOT NULL,
    raw_artifact_key                varchar(1024) NOT NULL,
    last_seen_at                    timestamptz   NOT NULL,
    superseded_at_version           bigint        NOT NULL,
    superseded_by_logical_input_id  varchar(256)  NOT NULL
);

CREATE TABLE IF NOT EXISTS public.ecp_curve_points (
    curve_id              varchar(64)   NOT NULL,
    kind                  varchar(16)   NOT NULL,
    as_of_date            date          NOT NULL,
    "position"            varchar(8)    NOT NULL,
    price                 numeric(18,4),
    status                varchar(8)    NOT NULL,
    gap_reason            varchar(512),
    estimate_type         varchar(32)   NOT NULL,
    shape_source          varchar(256)  NOT NULL,
    method_version        varchar(32)   NOT NULL,
    shape_method_version  varchar(32)   NOT NULL,
    params_sha256         varchar(64)   NOT NULL
);

-- One row per table per load: what was loaded and its hash, so a later session can see which
-- run and which dataset version the warehouse holds without reading S3.
CREATE TABLE IF NOT EXISTS public.ecp_load_log (
    table_name  varchar(64)   NOT NULL,
    source_id   varchar(256)  NOT NULL,
    row_count   bigint        NOT NULL,
    sha256      varchar(64)   NOT NULL,
    loaded_at   timestamptz   NOT NULL DEFAULT current_timestamp
);
