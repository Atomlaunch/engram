"""
Cross-session continuity — links related sessions and builds a
continuity thread injected at session bootstrap.

Three capabilities:
1. find_related_sessions  — find past sessions related to a topic
2. build_continuity_thread — build a compact multi-session summary
3. inject_continuity      — add continuity to the session briefing
"""

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable

from .query import full_text_search, get_sessions

logger = logging.getLogger("engram.continuity")


def _read_session_metadata(vault_root: str, rel_path: str) -> dict:
    """Read frontmatter from a session note."""
    path = Path(vault_root).expanduser() / rel_path
    try:
        import frontmatter
        post = frontmatter.load(str(path))
        return {
            "path": rel_path,
            "title": post.metadata.get("title", path.stem),
            "summary": post.metadata.get("summary", ""),
            "open_threads": post.metadata.get("open_threads", []),
            "entities": post.metadata.get("entities", []),
            "created": post.metadata.get("created", ""),
            "body": post.content.strip()[:500],
        }
    except Exception:
        return {"path": rel_path, "title": path.stem}


def find_related_sessions(
    conn,
    vault_root: str,
    topic: str,
    days: int = 14,
    limit: int = 5,
) -> list:
    """Find past sessions related to a topic.

    Uses FTS5 search across session notes, then enriches with metadata.

    Args:
        conn: SQLite connection.
        vault_root: Path to vault.
        topic: Search query (keywords, project name, etc.).
        days: How far back to look.
        limit: Max sessions to return.

    Returns:
        List of session metadata dicts, newest first.
    """
    # Search for topic in session-type notes
    results = full_text_search(conn, topic, types=["session"], limit=limit)

    if not results:
        # Try broader search
        results = full_text_search(conn, topic, limit=limit)

    related = []
    for r in results[:limit]:
        meta = _read_session_metadata(vault_root, r.get("path", ""))
        if meta.get("body") or meta.get("summary"):
            related.append(meta)

    return related


def build_continuity_thread(
    conn,
    vault_root: str,
    current_topic: Optional[str] = None,
    days: int = 7,
    max_sessions: int = 3,
    budget: int = 400,
) -> str:
    """Build a compact continuity summary from recent related sessions.

    This goes into the session briefing and provides cross-session context:
    what was being worked on, what's still open, what was decided.

    Args:
        conn: SQLite connection.
        vault_root: Path to vault.
        current_topic: Optional topic to focus the search.
        days: How many days of history to scan.
        max_sessions: Max past sessions to include.
        budget: Max chars for the output.

    Returns:
        Compact markdown continuity thread.
    """
    # Get recent sessions
    recent = get_sessions(conn, limit=max_sessions * 2)

    if not recent:
        return ""

    sections = []
    remaining = budget

    for s in recent[:max_sessions]:
        meta = _read_session_metadata(vault_root, s.get("path", ""))
        summary = meta.get("summary", "").strip()
        threads = meta.get("open_threads", [])
        created = meta.get("created", "")

        if not summary and not threads:
            # Use first line of body as summary
            body = meta.get("body", "")
            if body:
                summary = body.split("\n")[0].strip()[:120]

        if not summary and not threads:
            continue

        entry_parts = []
        if summary:
            entry_parts.append(summary[:150])
        if threads:
            thread_text = "; ".join(threads[:2])
            entry_parts.append(f"Open: {thread_text}")

        entry = " | ".join(entry_parts)
        date_label = created if created else s.get("title", "?")[:10]

        line = f"- [{date_label}] {entry}"
        if len(line) > remaining:
            line = line[:remaining - 3] + "..."
        sections.append(line)
        remaining -= len(line)

        if remaining <= 30:
            break

    if not sections:
        return ""

    # If there's a topic, try to find specifically related sessions
    if current_topic and remaining > 50:
        related = find_related_sessions(conn, vault_root, current_topic, days=days, limit=2)
        for r in related:
            # Don't duplicate sessions we already have
            if r["path"] in [s.get("path", "") for s in recent[:max_sessions]]:
                continue
            summary = r.get("summary", "") or r.get("body", "")[:100]
            if summary:
                line = f"- [related] {summary.strip()[:120]}"
                if len(line) <= remaining:
                    sections.append(line)
                    remaining -= len(line)

    header = "## Continuity\n"
    body = "\n".join(sections)
    full = header + body + "\n"

    if len(full) > budget:
        full = full[:budget - 3] + "...\n"

    return full


def prefetch_continuity(
    conn,
    vault_root: str,
    first_message: str = "",
    budget: int = 400,
) -> str:
    """Generate continuity context based on the first message of a new session.

    Called at session start to provide cross-session context relevant
    to what the user is asking about.

    Args:
        conn: SQLite connection.
        vault_root: Path to vault.
        first_message: The user's first message (used for topic extraction).
        budget: Max chars.

    Returns:
        Continuity markdown block.
    """
    # Extract topic keywords from first message
    topic = ""
    if first_message:
        # Simple keyword extraction — remove common words, take significant ones
        words = re.findall(r"\b\w{4,}\b", first_message.lower())
        stopwords = {"what", "how", "that", "this", "with", "from", "have",
                     "been", "were", "about", "would", "could", "should",
                     "there", "their", "which", "where", "when", "than",
                     "them", "then", "also", "just", "like", "know",
                     "going", "status", "update", "tell", "jarvis"}
        keywords = [w for w in words if w not in stopwords]
        topic = " ".join(keywords[:5])

    return build_continuity_thread(
        conn,
        vault_root,
        current_topic=topic if topic else None,
        days=7,
        max_sessions=3,
        budget=budget,
    )
