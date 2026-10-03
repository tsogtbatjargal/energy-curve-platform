-- Monitored values per dataset version (ADR-0013): for each curve and position, the latest curve
-- point of that version (price NULL for a gap). Outbox consumers resolve an event's values here,
-- by its own dataset_version, never from the latest-snapshot serving tables. Append-only: each
-- import adds its version's rows and nothing updates them; only a rebuild deletes and re-derives
-- them from the published artifacts.
CREATE TABLE market.monitored_values (
    dataset_version  integer       NOT NULL REFERENCES market.dataset_versions,
    curve_id         text          NOT NULL,
    position         text          NOT NULL,
    as_of_date       date          NOT NULL,
    price            numeric(18,4),
    status           text          NOT NULL CHECK (status IN ('ok', 'gap')),
    PRIMARY KEY (dataset_version, curve_id, position),
    CHECK ((status = 'ok') = (price IS NOT NULL))
);

CREATE FUNCTION market.reject_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END
$$;

CREATE TRIGGER monitored_values_append_only BEFORE UPDATE ON market.monitored_values
    FOR EACH STATEMENT EXECUTE FUNCTION market.reject_update();
