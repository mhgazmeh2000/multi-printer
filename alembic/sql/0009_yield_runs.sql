-- Version 1; migration-owned derived state, source payload stays in the current application schema.
-- yield-runs-statement
CREATE TABLE __APP_SCHEMA__.yield_run_meta (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version integer NOT NULL, ready boolean NOT NULL
);
-- yield-runs-statement
CREATE TABLE __APP_SCHEMA__.yield_run_slots (
    printer_ip text NOT NULL, slot_color text NOT NULL,
    last_id bigint, revision bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (printer_ip, slot_color)
);
-- yield-runs-statement
CREATE TABLE __APP_SCHEMA__.yield_run_boundaries (
    start_id bigint PRIMARY KEY, printer_ip text NOT NULL,
    slot_color text NOT NULL, previous_id bigint
);
-- yield-runs-statement
CREATE INDEX yield_runs_boundary_slot ON __APP_SCHEMA__.yield_run_boundaries
    (printer_ip, slot_color, start_id DESC);
