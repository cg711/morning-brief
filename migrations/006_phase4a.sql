CREATE TABLE daily_jobs (
  date             TEXT PRIMARY KEY,
  run_id           INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  status           TEXT NOT NULL CHECK (status IN ('waiting','claimed','speaking','done','failed','missed')),
  candidates_json  TEXT NOT NULL,
  bodies_json      TEXT NOT NULL,
  previous_json    TEXT NOT NULL,
  script_json      TEXT,
  cutoff_at        TEXT NOT NULL,
  claimed_at       TEXT,
  error            TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
