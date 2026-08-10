ALTER TABLE proposals ADD COLUMN workflow_id uuid;
ALTER TABLE conversations ADD COLUMN workflow_id uuid;
-- Legacy proposals cannot be resumed without a graph checkpoint.
UPDATE proposals SET state='superseded' WHERE state='pending';
