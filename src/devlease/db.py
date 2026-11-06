"""SQLite storage shared by every agent and the web service on one machine."""
import contextlib
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  native_id TEXT NOT NULL,
  name TEXT NOT NULL,
  profile TEXT NOT NULL,
  os TEXT NOT NULL DEFAULT '',
  port INTEGER,
  present INTEGER NOT NULL DEFAULT 1,
  UNIQUE(kind, native_id)
);
CREATE TABLE IF NOT EXISTS leases(
  id INTEGER PRIMARY KEY,
  device_id INTEGER NOT NULL REFERENCES devices(id),
  holder TEXT NOT NULL,
  pid INTEGER,
  note TEXT NOT NULL DEFAULT '',
  acquired_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  released_at REAL,
  end_reason TEXT
);
CREATE INDEX IF NOT EXISTS leases_active ON leases(device_id) WHERE released_at IS NULL;
CREATE TABLE IF NOT EXISTS waiters(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  profile TEXT,
  os TEXT,
  device TEXT,
  holder TEXT NOT NULL,
  pid INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  since REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  profile TEXT NOT NULL,
  os TEXT NOT NULL,
  image TEXT NOT NULL DEFAULT '',
  holder TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  result TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  decided_at REAL
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Limits are one set of numbers for the whole machine, changed with `devlease config`.
DEFAULTS = {
    "max_ttl": 1800,          # seconds: the longest lease or renewal
    "limit_ios": 2,           # simulators leased at once
    "limit_android": 2,       # emulators leased at once
    "max_devices_ios": 8,     # pool size above which a creation request needs a human
    "max_devices_android": 4,
    "min_free_gb": 20,        # free disk below which a creation request needs a human
    "port": 7788,
}


def state_dir() -> Path:
    path = Path(os.environ.get("DEVLEASE_DIR") or Path.home() / ".cache" / "devlease")
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(state_dir() / "devlease.db", timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    return con


@contextlib.contextmanager
def tx(con: sqlite3.Connection):
    """A write transaction that takes the lock up front, so two agents never interleave."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def cfg(con: sqlite3.Connection, key: str) -> int:
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return int(row["value"]) if row else DEFAULTS[key]
