"""
Query API — search and retrieve from the SQLite vault index.
All public functions return lists of dicts.
"""

import sqlite3
import logging
from typing import Optional

logger = logging.getLogger("engram.query")


def _row_to_dict(row) -> dict:
    return dict(row)


def search_facts(
    conn: sqlite3.Connection,
    query: str,
    artifact_type: Optional[str] = None,
    status: str = "active",
    limit: int = 10,
) -> list:
    """FTS search over facts, optionally filtered by artifact_type."""
    base = """
        SELECT d.path, d.title, d.subtype, d.status, d.importance, d.confidence,
               d.tags, d.created, d.updated,
               snippet(docs_fts, 1, '[', ']', '...', 15) as excerpt
        FROM docs_fts
        JOIN docs d ON docs_fts.rowid = d.id
        WHERE docs_fts MATCH ?
          AND d.type = 'fact'
          AND d.status = ?
        """
    params = [query, status]

    if artifact_type:
        base += " AND d.subtype = ?"
        params.append(artifact_type)

    base += " ORDER BY d.importance DESC, rank LIMIT ?"
    params.append(limit)

    try:
        return [_row_to_dict(r) for r in conn.execute(base, params).fetchall()]
    except sqlite3.OperationalError as e:
        logger.warning(f"search_facts error: {e}")
        return []


def search_entities(
    conn: sqlite3.Connection,
    name: Optional[str] = None,
    entity_type: Optional[str] = None,
    limit: int = 10,
) -> list:
    if name:
        rows = conn.execute("""
            SELECT d.path, d.title, d.subtype, d.importance, d.tags, d.created
            FROM docs_fts
            JOIN docs d ON docs_fts.rowid = d.id
            WHERE docs_fts MATCH ?
              AND d.type = 'entity'
            ORDER BY d.importance DESC, rank
            LIMIT ?
        """, (name, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT path, title, subtype, importance, tags, created
            FROM docs
            WHERE type = 'entity'
            ORDER BY importance DESC
            LIMIT ?
        """, (limit,)).fetchall()

    results = [_row_to_dict(r) for r in rows]
    if entity_type:
        results = [r for r in results if r.get("subtype") == entity_type]
    return results


def get_standing_rules(conn: sqlite3.Connection) -> list:
    """All active standing rules, always returned in full."""
    return [_row_to_dict(r) for r in conn.execute("""
        SELECT path, title, importance, confidence, created, updated
        FROM docs
        WHERE type = 'fact'
          AND subtype = 'standing_rule'
          AND status = 'active'
        ORDER BY importance DESC
    """).fetchall()]


def get_open_loops(conn: sqlite3.Connection, limit: int = 10) -> list:
    return [_row_to_dict(r) for r in conn.execute("""
        SELECT path, title, importance, confidence, created, updated
        FROM docs
        WHERE type = 'fact'
          AND subtype = 'open_loop'
          AND status = 'active'
        ORDER BY importance DESC
        LIMIT ?
    """, (limit,)).fetchall()]


def get_sessions(conn: sqlite3.Connection, limit: int = 5) -> list:
    return [_row_to_dict(r) for r in conn.execute("""
        SELECT path, title, created, updated, tags
        FROM docs
        WHERE type = 'session'
        ORDER BY created DESC
        LIMIT ?
    """, (limit,)).fetchall()]


def get_facts_about(
    conn: sqlite3.Connection,
    entity_name: str,
    limit: int = 20,
) -> list:
    """Facts linked to an entity via 'about' wikilinks."""
    return [_row_to_dict(r) for r in conn.execute("""
        SELECT d.path, d.title, d.subtype, d.status, d.importance, d.confidence, d.created
        FROM links l
        JOIN docs d ON l.from_path = d.path
        WHERE l.to_title = ?
          AND l.link_type = 'about'
          AND d.type = 'fact'
          AND d.status = 'active'
        ORDER BY d.importance DESC
        LIMIT ?
    """, (entity_name, limit)).fetchall()]


def full_text_search(
    conn: sqlite3.Connection,
    query: str,
    types: Optional[list] = None,
    limit: int = 20,
) -> list:
    """Broad FTS across all note types."""
    base = """
        SELECT d.path, d.title, d.type, d.subtype, d.status, d.importance,
               snippet(docs_fts, 1, '[', ']', '...', 15) as excerpt
        FROM docs_fts
        JOIN docs d ON docs_fts.rowid = d.id
        WHERE docs_fts MATCH ?
    """
    params = [query]

    if types:
        placeholders = ",".join("?" * len(types))
        base += f" AND d.type IN ({placeholders})"
        params.extend(types)

    base += " ORDER BY d.importance DESC, rank LIMIT ?"
    params.append(limit)

    try:
        return [_row_to_dict(r) for r in conn.execute(base, params).fetchall()]
    except sqlite3.OperationalError as e:
        logger.warning(f"full_text_search error: {e}")
        return []


def get_top_entities(conn: sqlite3.Connection, limit: int = 10) -> list:
    return [_row_to_dict(r) for r in conn.execute("""
        SELECT path, title, subtype, importance, tags, updated
        FROM docs
        WHERE type = 'entity'
          AND status = 'active'
        ORDER BY importance DESC, updated DESC
        LIMIT ?
    """, (limit,)).fetchall()]


def get_recent_facts(
    conn: sqlite3.Connection,
    days: int = 7,
    limit: int = 15,
) -> list:
    return [_row_to_dict(r) for r in conn.execute("""
        SELECT path, title, subtype, status, importance, confidence, created, updated
        FROM docs
        WHERE type = 'fact'
          AND status = 'active'
          AND date(updated) >= date('now', ? || ' days')
        ORDER BY importance DESC, updated DESC
        LIMIT ?
    """, (f"-{days}", limit)).fetchall()]
