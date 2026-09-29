CREATE TABLE notes (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  url TEXT,
  for_date TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_on TEXT
);
CREATE INDEX notes_pending ON notes(delivered_on, for_date);
CREATE TABLE countdowns (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL,
  date TEXT NOT NULL,
  created_at TEXT NOT NULL
);
