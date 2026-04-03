"""
SQLite schema for Engram vault index.
All migrations run here -- schema version tracked in index_meta.
"""

import sqlite3
from pathlib import Path

CURRENT_VERSION = "1.0.0"


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def create_schema(conn: sqlite3.Connection):
    conn.executescript("""
        -- Core docs table (all note types)
        CREATE TABLE IF NOT EXISTS docs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            path       TEXT UNIQUE NOT NULL,
            type       TEXT NOT NULL,          -- entity | fact | session
            subtype    TEXT,                   -- entity_type or artifact_type
            status     TEXT DEFAULT 'active',  -- active | superseded | archived
            importance REAL DEFAULT 0.5,
            confidence REAL DEFAULT 1.0,
            title      TEXT NOT NULL,
            tags       TEXT DEFAULT '',        -- comma-separated
            created    TEXT,
            updated    TEXT,
            mtime      REAL NOT NULL
        );

        -- FTS5 index (self-contained, stores its own copy of searchable text)
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            title,
            body,
            tags,
            tokenize='porter unicode61'
        );

        -- Wikilink graph
        CREATE TABLE IF NOT EXISTS links (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            from_path  TEXT NOT NULL,
            to_title   TEXT NOT NULL,
            link_type  TEXT DEFAULT 'body_link'  -- about | superseded_by | body_link | session_ref
        );
        CREATE INDEX IF NOT EXISTS links_from ON links(from_path);
        CREATE INDEX IF NOT EXISTS links_to ON links(to_title);

        -- Metadata / versioning
        CREATE TABLE IF NOT EXISTS index_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute(
        "SELECT value FROM index_meta WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str):
    conn.execute(
        "INSERT INTO index_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def init_db(db_path: str) -> sqlite3.Connection:
    """Create or open the index DB, run schema, return connection."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    create_schema(conn)
    if not get_meta(conn, "schema_version"):
        set_meta(conn, "schema_version", CURRENT_VERSION)
    return conn
