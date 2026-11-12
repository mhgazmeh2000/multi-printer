-- LOCAL PROTOTYPE ONLY. Installed by tools.yield_run_prototype in a fresh
-- printguard_test_* database, never as an application migration.
CREATE TABLE yield_run_lab.meta (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version integer NOT NULL, ready boolean NOT NULL
);
INSERT INTO yield_run_lab.meta VALUES (true, 1, false);
CREATE TABLE yield_run_lab.slots (
    printer_ip text NOT NULL, slot_color text NOT NULL,
    last_id bigint, revision bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (printer_ip, slot_color)
);
CREATE TABLE yield_run_lab.boundaries (
    start_id bigint PRIMARY KEY, printer_ip text NOT NULL,
    slot_color text NOT NULL, previous_id bigint
);
CREATE INDEX run_lab_boundary_slot ON yield_run_lab.boundaries
    (printer_ip, slot_color, start_id DESC);
CREATE INDEX run_lab_snapshot_neighbor ON yield_run_lab.toner_snapshots_v2
    (printer_ip, LOWER(color), id);

-- A permanent row per historical slot avoids delete/recreate (ABA) locks.
-- Updating revision is essential: a stale REPEATABLE READ writer must fail
-- serialization, not proceed after acquiring a lock with an obsolete snapshot.
CREATE FUNCTION yield_run_lab.lock_slots() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, yield_run_lab AS $$
DECLARE
    old_ip text; old_color text; new_ip text; new_color text; k record;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        old_ip := OLD.printer_ip; old_color := LOWER(OLD.color);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        new_ip := NEW.printer_ip; new_color := LOWER(NEW.color);
    END IF;
    FOR k IN
        SELECT DISTINCT v.ip, v.color FROM
            (VALUES (old_ip, old_color), (new_ip, new_color)) AS v(ip, color)
        WHERE v.ip IS NOT NULL ORDER BY v.ip, v.color
    LOOP
        INSERT INTO yield_run_lab.slots AS target (printer_ip, slot_color, revision)
        VALUES (k.ip, k.color, 1)
        ON CONFLICT (printer_ip, slot_color) DO UPDATE
            SET revision = target.revision + 1;
    END LOOP;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION yield_run_lab.repair_one(candidate bigint) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, yield_run_lab AS $$
DECLARE r record;
BEGIN
    SELECT s.id, s.printer_ip, LOWER(s.color) AS slot_color, p.id AS previous_id,
        (p.id IS NULL OR
         ROW(s.toner_level, s.raw_level, COALESCE(s.source, ''), s.valid,
             COALESCE(s.reject_reason, ''), COALESCE(s.cartridge_key, ''), s.full_color IS NULL)
         IS DISTINCT FROM
         ROW(p.toner_level, p.raw_level, COALESCE(p.source, ''), p.valid,
             COALESCE(p.reject_reason, ''), COALESCE(p.cartridge_key, ''), p.full_color IS NULL)
         OR s.usage_counter < p.usage_counter) AS changed
    INTO r
    FROM yield_run_lab.toner_snapshots_v2 s
    LEFT JOIN LATERAL (
        SELECT * FROM yield_run_lab.toner_snapshots_v2 p
        WHERE p.printer_ip = s.printer_ip AND LOWER(p.color) = LOWER(s.color)
          AND p.id < s.id ORDER BY p.id DESC LIMIT 1
    ) p ON true WHERE s.id = candidate;
    IF FOUND AND r.changed IS TRUE THEN
        INSERT INTO yield_run_lab.boundaries VALUES
            (r.id, r.printer_ip, r.slot_color, r.previous_id)
        ON CONFLICT (start_id) DO UPDATE SET
            printer_ip = EXCLUDED.printer_ip, slot_color = EXCLUDED.slot_color,
            previous_id = EXCLUDED.previous_id;
    ELSE
        DELETE FROM yield_run_lab.boundaries WHERE start_id = candidate;
    END IF;
END;
$$;

-- Transition tables give the final statement image, including bulk statements
-- and changing ids/slots. We do NOT assume row-trigger firing order follows id.
CREATE FUNCTION yield_run_lab.maintain() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, yield_run_lab AS $$
DECLARE positions text; r record;
BEGIN
    IF TG_OP = 'INSERT' THEN
        positions := 'SELECT id, printer_ip, LOWER(color) AS slot_color FROM new_rows';
    ELSIF TG_OP = 'DELETE' THEN
        positions := 'SELECT id, printer_ip, LOWER(color) AS slot_color FROM old_rows';
    ELSE
        positions := 'SELECT id, printer_ip, LOWER(color) AS slot_color FROM old_rows
                      UNION SELECT id, printer_ip, LOWER(color) AS slot_color FROM new_rows';
    END IF;
    -- Remove boundaries whose row disappeared or changed identity.
    EXECUTE format('DELETE FROM yield_run_lab.boundaries b
        WHERE b.start_id IN (SELECT id FROM (%s) a)
          AND NOT EXISTS (SELECT 1 FROM yield_run_lab.toner_snapshots_v2 s
              WHERE s.id = b.start_id AND s.printer_ip = b.printer_ip
                AND LOWER(s.color) = b.slot_color)', positions);
    -- For an existing position: itself and successor. For a deleted position:
    -- successor (plus one harmless extra row). DISTINCT repairs each row once.
    FOR r IN EXECUTE format('SELECT DISTINCT neighbor.id FROM (%s) a
        CROSS JOIN LATERAL (
            SELECT s.id FROM yield_run_lab.toner_snapshots_v2 s
            WHERE s.printer_ip = a.printer_ip AND LOWER(s.color) = a.slot_color
              AND s.id >= a.id ORDER BY s.id LIMIT 2
        ) neighbor ORDER BY neighbor.id', positions)
    LOOP
        PERFORM yield_run_lab.repair_one(r.id);
    END LOOP;
    FOR r IN EXECUTE format('SELECT DISTINCT printer_ip, slot_color FROM (%s) a
                            ORDER BY printer_ip, slot_color', positions)
    LOOP
        UPDATE yield_run_lab.slots st SET last_id = (
            SELECT s.id FROM yield_run_lab.toner_snapshots_v2 s
            WHERE s.printer_ip = r.printer_ip AND LOWER(s.color) = r.slot_color
            ORDER BY s.id DESC LIMIT 1
        ) WHERE st.printer_ip = r.printer_ip AND st.slot_color = r.slot_color;
    END LOOP;
    RETURN NULL;
END;
$$;

CREATE FUNCTION yield_run_lab.clear_runs() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, yield_run_lab AS $$
BEGIN
    TRUNCATE yield_run_lab.boundaries;
    UPDATE yield_run_lab.slots SET last_id = NULL, revision = revision + 1;
    RETURN NULL;
END;
$$;

CREATE TRIGGER run_lab_lock BEFORE INSERT OR UPDATE OR DELETE
ON yield_run_lab.toner_snapshots_v2 FOR EACH ROW
EXECUTE FUNCTION yield_run_lab.lock_slots();
CREATE TRIGGER run_lab_insert AFTER INSERT ON yield_run_lab.toner_snapshots_v2
REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT
EXECUTE FUNCTION yield_run_lab.maintain();
CREATE TRIGGER run_lab_delete AFTER DELETE ON yield_run_lab.toner_snapshots_v2
REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT
EXECUTE FUNCTION yield_run_lab.maintain();
CREATE TRIGGER run_lab_update AFTER UPDATE ON yield_run_lab.toner_snapshots_v2
REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT
EXECUTE FUNCTION yield_run_lab.maintain();
CREATE TRIGGER run_lab_truncate AFTER TRUNCATE ON yield_run_lab.toner_snapshots_v2
FOR EACH STATEMENT EXECUTE FUNCTION yield_run_lab.clear_runs();
