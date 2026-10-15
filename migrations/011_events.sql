CREATE TABLE event_mentions (
  title_key TEXT NOT NULL,
  title TEXT NOT NULL,
  date TEXT NOT NULL,
  mentioned_on TEXT NOT NULL,
  PRIMARY KEY (title_key, date)
);
CREATE INDEX event_mentions_date ON event_mentions(date);
