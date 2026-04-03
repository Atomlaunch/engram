"""Tests for engram.schema -- DB init, meta read/write, schema version."""

import sqlite3
import tempfile
import os
import pytest

from engram.schema import init_db, get_meta, set_meta, CURRENT_VERSION


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


def test_init_creates_tables(tmp_db):
    conn = init_db(tmp_db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "docs" in tables
    assert "links" in tables
    assert "index_meta" in tables


def test_init_creates_fts(tmp_db):
    conn = init_db(tmp_db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "docs_fts" in tables


def test_schema_version_set(tmp_db):
    conn = init_db(tmp_db)
    version = get_meta(conn, "schema_version")
    assert version == CURRENT_VERSION


def test_meta_roundtrip(tmp_db):
    conn = init_db(tmp_db)
    set_meta(conn, "test_key", "test_value")
    assert get_meta(conn, "test_key") == "test_value"


def test_meta_overwrite(tmp_db):
    conn = init_db(tmp_db)
    set_meta(conn, "key", "v1")
    set_meta(conn, "key", "v2")
    assert get_meta(conn, "key") == "v2"


def test_meta_missing_returns_default(tmp_db):
    conn = init_db(tmp_db)
    assert get_meta(conn, "nonexistent", "fallback") == "fallback"
    assert get_meta(conn, "nonexistent") is None


def test_wal_mode(tmp_db):
    conn = init_db(tmp_db)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_init_idempotent(tmp_db):
    """Calling init_db twice should not error or duplicate tables."""
    conn1 = init_db(tmp_db)
    conn1.close()
    conn2 = init_db(tmp_db)
    version = get_meta(conn2, "schema_version")
    assert version == CURRENT_VERSION
