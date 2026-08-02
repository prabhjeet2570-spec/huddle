CREATE TABLE outbox (
    id bigserial PRIMARY KEY,
    booking_id uuid NOT NULL REFERENCES bookings(id),
    version integer NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','processing','done','superseded','needs_review')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    lease_token uuid,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE(booking_id,version)
);
CREATE INDEX outbox_ready ON outbox(available_at) WHERE state IN ('pending','processing');
CREATE FUNCTION enqueue_calendar() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' OR NEW.version<>OLD.version THEN
        INSERT INTO outbox(booking_id,version) VALUES (NEW.id,NEW.version);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER booking_calendar AFTER INSERT OR UPDATE ON bookings FOR EACH ROW EXECUTE FUNCTION enqueue_calendar();
INSERT INTO outbox(booking_id,version) SELECT id,version FROM bookings;
CREATE TABLE worker_heartbeats (name text PRIMARY KEY, seen_at timestamptz NOT NULL);
