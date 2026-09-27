ALTER TABLE topics ADD COLUMN source_url TEXT;
ALTER TABLE topics ADD COLUMN fact_check INTEGER NOT NULL DEFAULT 0;
ALTER TABLE topics ADD COLUMN two_hosts INTEGER NOT NULL DEFAULT 0;

CREATE TABLE suggestions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  topic         TEXT NOT NULL,
  reason        TEXT NOT NULL DEFAULT '',
  from_topic_id INTEGER REFERENCES topics(id) ON DELETE SET NULL,
  status        TEXT NOT NULL CHECK (status IN ('new','added','dismissed')),
  created_at    TEXT NOT NULL
);
CREATE INDEX suggestions_status ON suggestions(status);
