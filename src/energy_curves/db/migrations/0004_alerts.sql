-- Threshold alerts (ADR-0013, ADR-0015). Owned by Postgres: backed up with db-backup.

CREATE TABLE app.alert_rules (
    rule_id           uuid          PRIMARY KEY,
    curve_id          text          NOT NULL,
    position          text          NOT NULL CHECK (position IN ('Spot', 'C1', 'C2', 'C3', 'C4')),
    threshold         numeric(18,4) NOT NULL,
    created_at        timestamptz   NOT NULL DEFAULT now(),
    deleted_at        timestamptz,
    -- Events for versions up to this one are never evaluated for the rule (its baseline).
    baseline_version  integer       NOT NULL CHECK (baseline_version >= 0),
    -- NULL until a non-gap value establishes the baseline; then "last value <= threshold".
    armed             boolean,
    last_value        numeric(18,4),
    last_as_of        date,
    last_version      integer
);

-- The alert log: one row per fired alert, and the durable, ordered stream behind
-- /api/alerts/events. seq is assigned under the alert-log lock, so commit order is seq order.
CREATE TABLE app.fired_alerts (
    seq              bigint        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alert_id         uuid          NOT NULL UNIQUE,
    rule_id          uuid          NOT NULL REFERENCES app.alert_rules,
    dataset_version  integer       NOT NULL,
    curve_id         text          NOT NULL,
    position         text          NOT NULL,
    as_of            date          NOT NULL,
    price            numeric(18,4) NOT NULL,
    previous_price   numeric(18,4),
    threshold        numeric(18,4) NOT NULL,
    fired_at         timestamptz   NOT NULL DEFAULT now(),
    UNIQUE (rule_id, dataset_version)
);

-- One row: the log's epoch (rotated by db-restore) and floor (highest pruned seq).
CREATE TABLE app.alert_log (
    singleton  boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    epoch      uuid    NOT NULL,
    floor      bigint  NOT NULL DEFAULT 0 CHECK (floor >= 0)
);
INSERT INTO app.alert_log (epoch) VALUES (gen_random_uuid());
