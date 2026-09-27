ALTER TABLE deep_dives ADD COLUMN chapters_json TEXT;
ALTER TABLE topics ADD COLUMN parent_topic_id INTEGER REFERENCES topics(id) ON DELETE SET NULL;
ALTER TABLE topics ADD COLUMN parent_section INTEGER;
