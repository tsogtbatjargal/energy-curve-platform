-- market: everything here is rebuildable from the published, hash-verified artifacts (ADR-0013).
CREATE SCHEMA market;

CREATE TABLE market.dataset_versions (
    dataset_version   integer PRIMARY KEY CHECK (dataset_version > 0),
    logical_input_id  text        NOT NULL UNIQUE,
    manifest_sha256   text        NOT NULL,
    source            text        NOT NULL,
    created_at        timestamptz NOT NULL,
    imported_at       timestamptz NOT NULL DEFAULT now(),
    observations      integer     NOT NULL,
    revisions         integer     NOT NULL,
    curve_points      integer     NOT NULL,
    price_changes     integer     NOT NULL
);

CREATE TABLE market.observations (
    source            text          NOT NULL,
    series_id         text          NOT NULL,
    observation_date  date          NOT NULL,
    price             numeric(18,6) NOT NULL,
    unit              text          NOT NULL,
    non_positive      boolean       NOT NULL,
    retrieved_at      timestamptz   NOT NULL,
    last_seen_at      timestamptz   NOT NULL,
    logical_input_id  text          NOT NULL,
    raw_artifact_key  text          NOT NULL,
    dataset_version   integer       NOT NULL REFERENCES market.dataset_versions,
    PRIMARY KEY (source, series_id, observation_date)
);

CREATE TABLE market.revisions (
    source                          text          NOT NULL,
    series_id                       text          NOT NULL,
    observation_date                date          NOT NULL,
    price                           numeric(18,6) NOT NULL,
    unit                            text          NOT NULL,
    non_positive                    boolean       NOT NULL,
    retrieved_at                    timestamptz   NOT NULL,
    last_seen_at                    timestamptz   NOT NULL,
    logical_input_id                text          NOT NULL,
    raw_artifact_key                text          NOT NULL,
    superseded_at_version           integer       NOT NULL,
    superseded_by_logical_input_id  text          NOT NULL,
    PRIMARY KEY (source, series_id, observation_date, superseded_at_version)
);

CREATE TABLE market.curve_points (
    curve_id              text          NOT NULL,
    kind                  text          NOT NULL,
    as_of_date            date          NOT NULL,
    position              text          NOT NULL,
    price                 numeric(18,4),
    status                text          NOT NULL CHECK (status IN ('ok', 'gap')),
    gap_reason            text,
    estimate_type         text          NOT NULL,
    shape_source          text          NOT NULL,
    method_version        text          NOT NULL,
    shape_method_version  text          NOT NULL,
    params_sha256         text          NOT NULL,
    dataset_version       integer       NOT NULL REFERENCES market.dataset_versions,
    PRIMARY KEY (curve_id, as_of_date, position),
    CHECK ((status = 'ok') = (price IS NOT NULL))
);

-- Every pipeline attempt, including failed and quarantined ones, for the Health tab.
CREATE TABLE market.pipeline_attempts (
    attempt_id        text PRIMARY KEY,
    logical_input_id  text        NOT NULL,
    status            text        NOT NULL,
    dataset_version   integer,
    quality           jsonb,
    merge             jsonb,
    error             text,
    duration_s        numeric,
    finished_at       timestamptz NOT NULL
);
CREATE INDEX pipeline_attempts_finished_at ON market.pipeline_attempts (finished_at DESC);
