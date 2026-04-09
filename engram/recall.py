"""
Recall — extended Engram recall with LLM-powered synthesis mode.

Wraps the query module's FTS5 search and adds an optional synthesis layer
that uses an LLM to produce a coherent answer from search results.
"""

import logging
import re
from pathlib import Path
from typing import Optional, Callable

from .query import full_text_search
from .dialectical import synthesize_recall

logger = logging.getLogger("engram.recall")


def recall(
    conn,
    vault_root: str,
    query: str,
    llm_call_fn: Optional[Callable[[str], str]] = None,
    type_filter: Optional[str] = None,
    limit: int = 5,
    synthesize: bool = False,
) -> dict:
    """Search the vault with optional LLM synthesis.

    Args:
        conn: SQLite connection to the vault index.
        vault_root: Path to Obsidian vault (for reading note bodies).
        query: Search query string.
        llm_call_fn: Callable for LLM synthesis (required if synthesize=True).
        type_filter: Optional filter — 'fact', 'entity', 'session'.
        limit: Max results to return.
        synthesize: If True, run LLM synthesis over results.

    Returns:
        Dict with 'results', 'synthesis', 'count'.
    """
    types = [type_filter] if type_filter else None

    # Phase 1: FTS5 search
    try:
        results = full_text_search(conn, query, types=types, limit=limit)
    except Exception as e:
        logger.warning("FTS search failed: %s", e)
        results = []

    # Enrich results with fact body content
    vault_path = Path(vault_root).expanduser()
    enriched = []
    for r in results:
        entry = dict(r)
        rel_path = r.get("path", "")
        try:
            full_path = vault_path / rel_path
            if full_path.exists():
                text = full_path.read_text(encoding="utf-8")
                body = re.sub(r"^---\n.*?\n---\n*", "", text, count=1, flags=re.DOTALL)
                first_line = body.strip().split("\n")[0].strip()[:200]
                if first_line:
                    entry["fact"] = first_line
        except Exception:
            pass
        enriched.append(entry)

    output = {
        "results": enriched,
        "count": len(enriched),
        "synthesis": "",
    }

    if not enriched:
        output["message"] = "No matches found."
        return output

    # Phase 2: Optional LLM synthesis
    if synthesize and llm_call_fn:
        try:
            synthesis_text = synthesize_recall(query, enriched, llm_call_fn)
            output["synthesis"] = synthesis_text
        except Exception as e:
            logger.warning("Recall synthesis failed: %s", e)
            output["synthesis"] = f"Synthesis unavailable: {e}"

    return output
