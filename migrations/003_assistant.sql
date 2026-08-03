CREATE TABLE proposals (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES sessions(id),
    arguments jsonb NOT NULL,
    expires_at timestamptz NOT NULL DEFAULT now()+interval '5 minutes',
    state text NOT NULL DEFAULT 'pending',
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE conversations (
    owner_id uuid PRIMARY KEY REFERENCES sessions(id),
    messages jsonb NOT NULL DEFAULT '[]'
);
CREATE TABLE ai_runs (
    id bigserial PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES sessions(id),
    model text NOT NULL,
    tokens integer NOT NULL DEFAULT 0,
    elapsed_ms integer NOT NULL DEFAULT 0,
    outcome text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
