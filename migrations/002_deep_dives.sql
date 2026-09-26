CREATE TABLE topics (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  topic       TEXT NOT NULL,
  notes       TEXT NOT NULL DEFAULT '',
  position    REAL NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('queued','researching','speaking','ready','heard','failed')),
  claimed_at  TEXT,
  error       TEXT,
  script_json TEXT,
  render_attempts INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE deep_dives (
  topic_id     INTEGER PRIMARY KEY REFERENCES topics(id) ON DELETE CASCADE,
  title        TEXT NOT NULL,
  word_count   INTEGER NOT NULL,
  duration_s   REAL NOT NULL,
  audio_bytes  INTEGER NOT NULL,
  published_at TEXT NOT NULL,
  heard_at     TEXT,
  updated_at   TEXT NOT NULL
);

CREATE TABLE deep_dive_sources (
  topic_id  INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL,
  title     TEXT NOT NULL,
  publisher TEXT NOT NULL DEFAULT '',
  url       TEXT NOT NULL,
  PRIMARY KEY (topic_id, source_id)
);

CREATE INDEX topics_status ON topics(status);
