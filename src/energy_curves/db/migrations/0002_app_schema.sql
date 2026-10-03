-- app: owned by Postgres, not rebuildable from artifacts; backed up with db-backup (ADR-0013).
CREATE SCHEMA app;

-- Transactional outbox. Written in the same transaction as each import; consumers mark an event
-- done in the same transaction as their own effects.
CREATE TABLE app.outbox_events (
    event_id         uuid        PRIMARY KEY,
    event_type       text        NOT NULL,
    dataset_version  integer     NOT NULL,
    payload          jsonb       NOT NULL,
    status           text        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'dead')),
    attempts         integer     NOT NULL DEFAULT 0,
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    processed_at     timestamptz,
    UNIQUE (event_type, dataset_version)
);
CREATE INDEX outbox_pending ON app.outbox_events (event_type, dataset_version) WHERE status = 'pending';
