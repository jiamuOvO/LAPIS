ALTER TABLE research_tasks ADD COLUMN active_request_version INTEGER;
ALTER TABLE research_tasks ADD CONSTRAINT active_request_fk FOREIGN KEY (id,active_request_version) REFERENCES request_versions(task_id,version);
ALTER TABLE intake_operations ADD COLUMN input_context JSONB NOT NULL DEFAULT '{}';
