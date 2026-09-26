CREATE TABLE episodes (
  date          TEXT PRIMARY KEY,
  cutoff_at     TEXT NOT NULL,
  script_json   TEXT NOT NULL,
  word_count    INTEGER NOT NULL,
  duration_s    REAL NOT NULL,
  audio_bytes   INTEGER NOT NULL,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE sources (
  episode_date  TEXT NOT NULL REFERENCES episodes(date) ON DELETE CASCADE,
  item_id       TEXT NOT NULL,
  source        TEXT NOT NULL,
  title         TEXT NOT NULL,
  url           TEXT NOT NULL,
  published_at  TEXT NOT NULL,
  PRIMARY KEY (episode_date, item_id)
);

CREATE TABLE runs (
  id            INTEGER PRIMARY KEY,
  date          TEXT NOT NULL,
  trigger       TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
  stage         TEXT,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  error         TEXT,
  feed_errors   TEXT NOT NULL DEFAULT '[]',
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  model         TEXT NOT NULL
);

CREATE INDEX runs_date ON runs(date);
