CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE TABLE sessions (
    id uuid PRIMARY KEY,
    token_hash text UNIQUE NOT NULL,
    name text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE TABLE bookings (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES sessions(id),
    room_id text NOT NULL CHECK (room_id IN ('cedar','maple','birch')),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 100),
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL CHECK (ends_at > starts_at),
    attendees integer NOT NULL CHECK (attendees > 0 AND attendees <= CASE room_id WHEN 'cedar' THEN 4 WHEN 'maple' THEN 8 WHEN 'birch' THEN 12 END),
    status text NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed','cancelled')),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    cancelled_at timestamptz,
    calendar_status text NOT NULL DEFAULT 'pending' CHECK (calendar_status IN ('pending','synced','needs_review')),
    EXCLUDE USING gist (room_id WITH =, tstzrange(starts_at,ends_at,'[)') WITH &&) WHERE (status='confirmed')
);
CREATE TABLE requests (
    owner_id uuid NOT NULL REFERENCES sessions(id),
    key text NOT NULL,
    fingerprint text NOT NULL,
    response jsonb NOT NULL,
    PRIMARY KEY(owner_id,key)
);
CREATE TABLE booking_events (
    id bigserial PRIMARY KEY,
    booking_id uuid NOT NULL REFERENCES bookings(id),
    kind text NOT NULL,
    detail text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX bookings_owner ON bookings(owner_id, starts_at);
