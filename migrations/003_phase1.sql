CREATE TABLE app_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

ALTER TABLE topics ADD COLUMN long_notified TEXT;
