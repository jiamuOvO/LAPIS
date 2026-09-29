ALTER TABLE schema_migrations ADD COLUMN checksum CHAR(64);
ALTER TABLE schema_migrations ADD COLUMN description TEXT;
CREATE INDEX ix_intake_operations_task_revision ON intake_operations(task_id, revision);
