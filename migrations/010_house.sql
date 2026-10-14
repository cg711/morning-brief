CREATE TABLE house_members (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL, revoked_at TEXT);
CREATE TABLE house_chores (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, weekday INTEGER NOT NULL,
  start_date TEXT NOT NULL, rotation_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE house_chore_done (
  chore_id INTEGER NOT NULL REFERENCES house_chores(id) ON DELETE CASCADE,
  day TEXT NOT NULL, done_by INTEGER, done_at TEXT NOT NULL, PRIMARY KEY (chore_id, day));
CREATE TABLE house_todos (
  id INTEGER PRIMARY KEY, text TEXT NOT NULL, added_by INTEGER, claimed_by INTEGER, due TEXT,
  created_at TEXT NOT NULL, announced_on TEXT, done_at TEXT, done_by INTEGER);
CREATE TABLE house_events (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, day TEXT NOT NULL, host_id INTEGER, created_at TEXT NOT NULL);
CREATE TABLE house_prep (
  id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES house_events(id) ON DELETE CASCADE,
  text TEXT NOT NULL, position INTEGER NOT NULL, done_at TEXT, done_by INTEGER);
CREATE TABLE house_upkeep (
  id INTEGER PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL, every_days INTEGER, month INTEGER,
  day INTEGER, last_done TEXT, created_at TEXT NOT NULL);
CREATE TABLE house_notes (
  id INTEGER PRIMARY KEY, member_id INTEGER, text TEXT NOT NULL, created_at TEXT NOT NULL, announced_on TEXT);
CREATE TABLE house_memos (
  id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL, duration_s REAL NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, approved_at TEXT, played_on TEXT);
CREATE TABLE house_episodes (
  date TEXT PRIMARY KEY, script_json TEXT NOT NULL, duration_s REAL NOT NULL, audio_bytes INTEGER NOT NULL,
  word_count INTEGER NOT NULL, chapters_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
