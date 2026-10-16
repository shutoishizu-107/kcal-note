import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','user')),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended')),
  created_at TEXT NOT NULL,
  created_by INTEGER
);
CREATE TABLE IF NOT EXISTS invites(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK(role IN ('admin','user')),
  created_by INTEGER,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  used_by INTEGER
);
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ip TEXT NOT NULL,
  username TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bucket TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hevy_connections(
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  key_enc TEXT NOT NULL,
  key_hint TEXT NOT NULL,
  hevy_account_id TEXT NOT NULL,
  hevy_username TEXT,
  hevy_name TEXT,
  auto_sync INTEGER NOT NULL DEFAULT 1,
  initial_days INTEGER NOT NULL DEFAULT 90,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hevy_state(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  initial_from TEXT,
  cursor_since TEXT,
  initial_done INTEGER NOT NULL DEFAULT 0,
  last_success_at TEXT,
  last_attempt_at TEXT,
  last_error TEXT,
  syncing_since TEXT,
  templates_at TEXT,
  PRIMARY KEY(user_id, account_id)
);
CREATE TABLE IF NOT EXISTS hevy_workouts(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL,
  workout_id TEXT NOT NULL,
  payload TEXT,
  hevy_updated_at TEXT,
  start_time TEXT,
  deleted INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id, account_id, workout_id)
);
CREATE TABLE IF NOT EXISTS hevy_changes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  account_id TEXT NOT NULL,
  workout_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('upsert','delete'))
);
CREATE INDEX IF NOT EXISTS idx_changes ON hevy_changes(user_id, account_id, id);
CREATE TABLE IF NOT EXISTS hevy_templates(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL,
  template_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY(user_id, account_id, template_id)
);
"""


def connect(path):
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # 手動トランザクション
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.close()
