"""
Vault indexer — scans markdown files, parses frontmatter,
keeps SQLite FTS index in sync. Supports full scan and incremental.
File watcher keeps index live during a session.
"""

import os
import re
import logging
import threading
from pathlib import Path
from typing import Optional

import frontmatter
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

from .schema import init_db, get_meta, set_meta

logger = logging.getLogger("engram.indexer")

# Fields we store as columns (everything else goes into FTS body)
FRONTMATTER_COLS = {
    "type", "entity_type", "artifact_type",
    "status", "importance", "confidence",
    "tags", "created", "updated",
}


def _parse_note(path: str, vault_root: str):
    """Parse a markdown note into a dict suitable for DB insert."""
    rel_path = os.path.relpath(path, vault_root)
    mtime = os.path.getmtime(path)

    try:
        post = frontmatter.load(path)
    except Exception as e:
        logger.warning(f"Failed to parse {rel_path}: {e}")
        return None

    meta = post.metadata
    body = post.content

    note_type = meta.get("type", _infer_type(rel_path))
    subtype = meta.get("artifact_type") or meta.get("entity_type")

    # Title: frontmatter title > name (for entities) > first H1 > filename stem
    title = meta.get("title") or meta.get("name") or _extract_h1(body) or Path(path).stem

    tags_raw = meta.get("tags", [])
    if isinstance(tags_raw, list):
        tags = ",".join(str(t) for t in tags_raw)
    else:
        tags = str(tags_raw)

    importance = float(meta.get("importance", 0.5))
    confidence = float(meta.get("confidence", 1.0))
    status = meta.get("status", "active")
    created = meta.get("created", "")
    updated = meta.get("updated", "")

    # Extract wikilinks from body + frontmatter arrays
    links = _extract_links(body, meta)

    return {
        "path": rel_path,
        "type": note_type,
        "subtype": subtype,
        "status": str(status),
        "importance": importance,
        "confidence": confidence,
        "title": title,
        "tags": tags,
        "created": str(created) if created else "",
        "updated": str(updated) if updated else "",
        "mtime": mtime,
        "body": body,
        "links": links,
    }


def _infer_type(rel_path: str) -> str:
    parts = rel_path.lower().split(os.sep)
    if "entities" in parts:
        return "entity"
    if "facts" in parts:
        return "fact"
    if "sessions" in parts:
        return "session"
    return "note"


def _extract_h1(body: str) -> Optional[str]:
    m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    return m.group(1).strip() if m else None


def _extract_links(body: str, meta: dict) -> list:
    """Return list of (to_title, link_type) tuples."""
    links = []

    # Wikilinks in body
    for match in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", body):
        links.append((match.group(1).strip(), "body_link"))

    # Typed frontmatter links
    for field, link_type in [
        ("about", "about"),
        ("superseded_by", "superseded_by"),
        ("entities_referenced", "session_ref"),
    ]:
        val = meta.get(field)
        if not val:
            continue
        if isinstance(val, list):
            for item in val:
                title = re.sub(r"^\[\[|\]\]$", "", str(item)).strip()
                if title:
                    links.append((title, link_type))
        elif isinstance(val, str):
            title = re.sub(r"^\[\[|\]\]$", "", val).strip()
            if title:
                links.append((title, link_type))

    return links


def _upsert_note(conn, note: dict):
    """Insert or update a note in docs + FTS + links."""
    existing = conn.execute(
        "SELECT id, mtime FROM docs WHERE path = ?", (note["path"],)
    ).fetchone()

    if existing and abs(existing["mtime"] - note["mtime"]) < 0.01:
        return False  # unchanged

    if existing:
        conn.execute("""
            UPDATE docs SET
                type=?, subtype=?, status=?, importance=?, confidence=?,
                title=?, tags=?, created=?, updated=?, mtime=?
            WHERE path=?
        """, (
            note["type"], note["subtype"], note["status"],
            note["importance"], note["confidence"],
            note["title"], note["tags"],
            note["created"], note["updated"], note["mtime"],
            note["path"],
        ))
        rowid = existing["id"]
        # Update FTS: delete old then insert new
        conn.execute(
            "DELETE FROM docs_fts WHERE rowid = ?", (rowid,)
        )
        conn.execute(
            "INSERT INTO docs_fts(rowid, title, body, tags) VALUES(?,?,?,?)",
            (rowid, note["title"], note["body"], note["tags"])
        )
    else:
        conn.execute("""
            INSERT INTO docs(path, type, subtype, status, importance, confidence,
                             title, tags, created, updated, mtime)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (
            note["path"], note["type"], note["subtype"], note["status"],
            note["importance"], note["confidence"],
            note["title"], note["tags"],
            note["created"], note["updated"], note["mtime"],
        ))
        rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO docs_fts(rowid, title, body, tags) VALUES(?,?,?,?)",
            (rowid, note["title"], note["body"], note["tags"])
        )

    # Refresh links
    conn.execute("DELETE FROM links WHERE from_path = ?", (note["path"],))
    for to_title, link_type in note["links"]:
        conn.execute(
            "INSERT INTO links(from_path, to_title, link_type) VALUES(?,?,?)",
            (note["path"], to_title, link_type)
        )

    return True


def _delete_note(conn, rel_path: str):
    row = conn.execute("SELECT id FROM docs WHERE path=?", (rel_path,)).fetchone()
    if row:
        conn.execute("DELETE FROM docs_fts WHERE rowid = ?", (row["id"],))
        conn.execute("DELETE FROM docs WHERE path=?", (rel_path,))
        conn.execute("DELETE FROM links WHERE from_path=?", (rel_path,))


def full_index(conn, vault_root: str) -> dict:
    """Walk the entire vault and index all markdown files."""
    vault_root = str(Path(vault_root).expanduser())
    stats = {"new": 0, "updated": 0, "skipped": 0, "errors": 0}

    all_paths = set()
    for root, _, files in os.walk(vault_root):
        for fname in files:
            if not fname.endswith(".md") or fname.startswith("."):
                continue
            fpath = os.path.join(root, fname)
            all_paths.add(os.path.relpath(fpath, vault_root))
            note = _parse_note(fpath, vault_root)
            if note is None:
                stats["errors"] += 1
                continue
            changed = _upsert_note(conn, note)
            if changed:
                existing = conn.execute(
                    "SELECT id FROM docs WHERE path=?", (note["path"],)
                ).fetchone()
                if existing:
                    stats["updated"] += 1
                else:
                    stats["new"] += 1
            else:
                stats["skipped"] += 1

    # Remove deleted files
    indexed = {
        row[0] for row in conn.execute("SELECT path FROM docs").fetchall()
    }
    for stale in indexed - all_paths:
        _delete_note(conn, stale)

    conn.commit()
    import datetime
    set_meta(conn, "last_indexed", datetime.datetime.now(datetime.timezone.utc).isoformat())
    return stats


class _VaultEventHandler(FileSystemEventHandler):
    def __init__(self, conn, vault_root: str):
        self.conn = conn
        self.vault_root = vault_root
        self._timers = {}

    def _schedule(self, path: str, op: str):
        if path in self._timers:
            self._timers[path].cancel()
        t = threading.Timer(0.5, self._handle, args=[path, op])
        self._timers[path] = t
        t.start()

    def _handle(self, path: str, op: str):
        self._timers.pop(path, None)
        rel = os.path.relpath(path, self.vault_root)
        if op == "delete":
            _delete_note(self.conn, rel)
            self.conn.commit()
            logger.debug(f"Removed: {rel}")
        else:
            note = _parse_note(path, self.vault_root)
            if note:
                _upsert_note(self.conn, note)
                self.conn.commit()
                logger.debug(f"Indexed: {rel}")

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            self._schedule(event.src_path, "upsert")

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            self._schedule(event.src_path, "upsert")

    def on_deleted(self, event):
        if event.src_path.endswith(".md"):
            self._schedule(event.src_path, "delete")

    def on_moved(self, event):
        if event.src_path.endswith(".md"):
            self._schedule(event.src_path, "delete")
        if event.dest_path.endswith(".md"):
            self._schedule(event.dest_path, "upsert")


def start_watcher(conn, vault_root: str):
    """Start a background file watcher. Returns the observer."""
    vault_root = str(Path(vault_root).expanduser())
    handler = _VaultEventHandler(conn, vault_root)

    # Auto-detect: /mnt/ paths need polling (WSL2 Windows mounts)
    if os.path.realpath(vault_root).startswith("/mnt/"):
        logger.info("WSL2 Windows mount detected -- using PollingObserver")
        observer = PollingObserver(timeout=3)
    else:
        observer = Observer()

    observer.schedule(handler, vault_root, recursive=True)
    observer.start()
    logger.info(f"Watching vault: {vault_root}")
    return observer
