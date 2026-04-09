"""
Dialectical Layer — cross-session memory synthesis for Engram.

Three capabilities:
1. archive_raw_session  — write raw conversation logs to vault for synthesis
2. synthesize_sessions  — dream pass: extract new facts/entities from raw sessions
3. apply_synthesis      — write synthesis output to vault as structured notes
4. synthesize_recall    — query-time LLM synthesis over search results
"""

import json
import logging
import os
import re
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger("engram.dialectical")

# Max chars per message in raw archive (keep compact but complete enough for synthesis)
MAX_MSG_CHARS = 2000

# ---------------------------------------------------------------------------
# 1. Session Archiver
# ---------------------------------------------------------------------------

def archive_raw_session(
    vault_root: str,
    session_id: str,
    messages: list,
    platform: str = "cli",
    channel_name: str = "",
) -> str:
    """Archive raw conversation to vault/Memory/Sessions/Raw/.

    Args:
        vault_root: Path to Obsidian vault root.
        session_id: Unique session identifier.
        messages: List of dicts with 'role' and 'content' keys.
        platform: Where the conversation happened (cli, discord, telegram).
        channel_name: Optional channel/thread name.

    Returns:
        Path to the written file.
    """
    vault_root = Path(vault_root).expanduser()
    raw_dir = vault_root / "Memory" / "Sessions" / "Raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(tz=None)  # Uses local time, compatible with Python 3.12+
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M")

    # Build conversation markdown
    turns = []
    user_count = 0
    assistant_count = 0
    for m in messages:
        role = m.get("role", "").upper()
        content = m.get("content", "")
        if not content or not isinstance(content, str):
            continue
        if role == "USER":
            user_count += 1
        elif role == "ASSISTANT":
            assistant_count += 1
        else:
            continue
        turns.append(f"**{role}**: {content[:MAX_MSG_CHARS]}")

    if not turns:
        logger.debug("No turns to archive for session %s", session_id)
        return ""

    conversation = "\n\n---\n\n".join(turns)
    total_messages = user_count + assistant_count

    # Build frontmatter
    content = f"""---
type: raw_session
session_id: {session_id}
platform: {platform}
channel: {channel_name}
created: {date_str}
message_count: {total_messages}
user_turns: {user_count}
assistant_turns: {assistant_count}
---

# Session {date_str} {time_str}

> Platform: {platform} | Messages: {total_messages} | ID: {session_id}

{conversation}
"""

    filename = f"{date_str}-{time_str}-{platform}-{(session_id or 'unknown')[:8]}.md"
    filepath = raw_dir / filename
    filepath.write_text(content, encoding="utf-8")
    logger.info("Raw session archived: %s (%d turns)", filepath.name, total_messages)
    return str(filepath)


# ---------------------------------------------------------------------------
# 2. Dream Mode — Session Synthesis
# ---------------------------------------------------------------------------

SYNTHESIS_PROMPT = """\
You are analyzing recent AI agent conversation sessions to extract durable knowledge.

GOAL: Identify NEW facts and entities that are NOT already stored. Also identify
any facts that need UPDATING based on new information.

EXISTING FACTS (already stored — do NOT re-extract these):
{existing_facts}

EXISTING ENTITIES (already known):
{existing_entities}

RECENT SESSIONS:
{session_text}

OUTPUT: Valid JSON only. No markdown fences. No explanation.

{
  "new_facts": [
    {
      "title": "short descriptive title",
      "content": "the fact itself, one clear statement",
      "artifact_type": "durable_fact|preference|decision|lesson|constraint",
      "confidence": 0.0-1.0,
      "importance": 0.0-1.0,
      "about": ["Entity Name"],
      "tags": ["tag1"]
    }
  ],
  "updated_facts": [
    {
      "title": "title of existing fact",
      "update": "what changed or the new information to append",
      "reason": "why this update is needed"
    }
  ],
  "new_entities": [
    {
      "name": "Entity Name",
      "entity_type": "person|project|tool|concept|place|org",
      "description": "brief description",
      "importance": 0.0-1.0,
      "tags": ["tag1"]
    }
  ],
  "synthesis": "1-2 sentence summary of what was learned from these sessions"
}

RULES:
- Only extract facts worth keeping months from now
- Prefer quality over quantity — 3 strong facts beat 15 weak ones
- confidence < 0.6 means you're not sure — discard
- Do NOT extract temporary status or obvious things
- If nothing new was learned, return empty arrays
"""


def _load_raw_sessions(vault_root: Path, days: int = 1) -> list:
    """Load raw session files from the last N days."""
    raw_dir = vault_root / "Memory" / "Sessions" / "Raw"
    if not raw_dir.exists():
        return []

    cutoff = datetime.now() - timedelta(days=days)
    sessions = []

    for fpath in sorted(raw_dir.glob("*.md"), reverse=True):
        try:
            mtime_ts = fpath.stat().st_mtime
            mtime = datetime.fromtimestamp(mtime_ts)
            if mtime < cutoff:
                continue
            text = fpath.read_text(encoding="utf-8")
            # Strip frontmatter, keep body
            body = re.sub(r"^---\n.*?\n---\n*", "", text, count=1, flags=re.DOTALL)
            if body.strip():
                sessions.append({
                    "filename": fpath.name,
                    "date": mtime.strftime("%Y-%m-%d %H:%M"),
                    "content": body.strip(),
                })
        except Exception as e:
            logger.warning("Failed to read raw session %s: %s", fpath.name, e)

    return sessions


def _load_existing_facts_summary(vault_root: Path, limit: int = 50) -> str:
    """Load a compact summary of existing facts for dedup context."""
    facts_dir = vault_root / "Memory" / "Facts"
    if not facts_dir.exists():
        return "(none)"

    lines = []
    for fpath in sorted(facts_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            text = fpath.read_text(encoding="utf-8")
            # Extract title from frontmatter or filename
            fm_match = re.search(r"artifact_type:\s*(\w+)", text)
            artifact = fm_match.group(1) if fm_match else "fact"
            body = re.sub(r"^---\n.*?\n---\n*", "", text, count=1, flags=re.DOTALL)
            first_line = body.strip().split("\n")[0].strip()[:120]
            if first_line:
                lines.append(f"- [{artifact}] {first_line}")
        except Exception:
            pass

    return "\n".join(lines) if lines else "(none)"


def _load_existing_entities_summary(vault_root: Path, limit: int = 30) -> str:
    """Load a compact summary of existing entities."""
    entities_dir = vault_root / "Memory" / "Entities"
    if not entities_dir.exists():
        return "(none)"

    lines = []
    for fpath in sorted(entities_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            text = fpath.read_text(encoding="utf-8")
            name_match = re.search(r"name:\s*(.+)", text)
            type_match = re.search(r"entity_type:\s*(\w+)", text)
            name = name_match.group(1).strip() if name_match else fpath.stem
            etype = type_match.group(1) if type_match else "entity"
            lines.append(f"- {name} ({etype})")
        except Exception:
            pass

    return "\n".join(lines) if lines else "(none)"


def synthesize_sessions(
    vault_root: str,
    llm_call_fn: Callable[[str], str],
    days: int = 1,
) -> dict:
    """Dream pass: synthesize raw sessions into new facts/entities.

    Args:
        vault_root: Path to Obsidian vault.
        llm_call_fn: Callable that takes a prompt string, returns LLM response string.
        days: How many days of raw sessions to process.

    Returns:
        Dict with new_facts, updated_facts, new_entities, synthesis.
    """
    vault_root = Path(vault_root).expanduser()

    # Load raw sessions
    sessions = _load_raw_sessions(vault_root, days=days)
    if not sessions:
        logger.info("No raw sessions to synthesize")
        return {
            "new_facts": [],
            "updated_facts": [],
            "new_entities": [],
            "synthesis": "No sessions to process.",
            "sessions_scanned": 0,
        }

    logger.info("Synthesizing %d raw sessions from last %d days", len(sessions), days)

    # Build session text (truncate if too long)
    session_parts = []
    total_chars = 0
    max_chars = 12000  # Leave room for the rest of the prompt
    for s in sessions:
        entry = f"--- Session: {s['date']} ({s['filename']}) ---\n{s['content']}"
        if total_chars + len(entry) > max_chars:
            entry = entry[:max_chars - total_chars] + "\n[TRUNCATED]"
            session_parts.append(entry)
            break
        session_parts.append(entry)
        total_chars += len(entry)

    session_text = "\n\n".join(session_parts)
    existing_facts = _load_existing_facts_summary(vault_root)
    existing_entities = _load_existing_entities_summary(vault_root)

    prompt = SYNTHESIS_PROMPT.format(
        existing_facts=existing_facts,
        existing_entities=existing_entities,
        session_text=session_text,
    )

    # Call LLM
    try:
        raw = llm_call_fn(prompt)
    except Exception as e:
        logger.error("LLM call failed in synthesize_sessions: %s", e)
        return {
            "new_facts": [],
            "updated_facts": [],
            "new_entities": [],
            "synthesis": f"LLM call failed: {e}",
            "sessions_scanned": len(sessions),
        }

    # Parse response
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse synthesis JSON: %s", e)
        return {
            "new_facts": [],
            "updated_facts": [],
            "new_entities": [],
            "synthesis": f"Parse error: {e}",
            "sessions_scanned": len(sessions),
            "raw_response": raw[:500],
        }

    result["sessions_scanned"] = len(sessions)
    logger.info(
        "Synthesis complete: %d new facts, %d updated, %d new entities",
        len(result.get("new_facts", [])),
        len(result.get("updated_facts", [])),
        len(result.get("new_entities", [])),
    )
    return result


# ---------------------------------------------------------------------------
# 3. Apply Synthesis — Write to Vault
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text)
    return text[:60]


def apply_synthesis(
    vault_root: str,
    synthesis_result: dict,
) -> dict:
    """Write synthesis output to the vault as structured notes.

    Creates new fact notes, new entity notes, and appends updates to existing facts.
    Never deletes — only adds/appends.

    Args:
        vault_root: Path to Obsidian vault.
        synthesis_result: Output from synthesize_sessions().

    Returns:
        Stats dict with counts of created/updated items.
    """
    vault_root = Path(vault_root).expanduser()
    today = date.today().isoformat()
    stats = {
        "facts_created": 0,
        "entities_created": 0,
        "facts_updated": 0,
        "errors": 0,
    }

    # Create new facts
    for fact in synthesis_result.get("new_facts", []):
        try:
            title = fact.get("title", "").strip()
            content_body = fact.get("content", "").strip()
            artifact_type = fact.get("artifact_type", "durable_fact")
            confidence = float(fact.get("confidence", 0.7))
            importance = float(fact.get("importance", 0.5))

            if not title or confidence < 0.6:
                continue

            if artifact_type == "standing_rule":
                importance = 1.0

            facts_dir = vault_root / "Memory" / "Facts"
            facts_dir.mkdir(parents=True, exist_ok=True)

            slug = _slugify(title)
            filepath = facts_dir / f"{today}-{slug}.md"

            if filepath.exists():
                logger.debug("Fact already exists: %s", filepath.name)
                continue

            about = fact.get("about", [])
            about_yaml = "\n".join(f"  - '[[{e}]]'" for e in about) if about else "  []"
            tags = fact.get("tags", [])
            tags_yaml = "[" + ", ".join(tags) + "]" if tags else "[]"

            note = f"""---
type: fact
artifact_type: {artifact_type}
status: active
importance: {importance:.1f}
confidence: {confidence:.2f}
about:
{about_yaml}
tags: {tags_yaml}
source: dream-synthesis
created: {today}
updated: {today}
---

{content_body}
"""
            filepath.write_text(note, encoding="utf-8")
            stats["facts_created"] += 1
            logger.info("Fact created: %s", filepath.name)
        except Exception as e:
            logger.warning("Failed to create fact '%s': %s", fact.get("title", "?"), e)
            stats["errors"] += 1

    # Create new entities
    for entity in synthesis_result.get("new_entities", []):
        try:
            name = entity.get("name", "").strip()
            if not name:
                continue

            entities_dir = vault_root / "Memory" / "Entities"
            entities_dir.mkdir(parents=True, exist_ok=True)

            slug = _slugify(name)
            filepath = entities_dir / f"{slug}.md"

            if filepath.exists():
                logger.debug("Entity already exists: %s", filepath.name)
                continue

            tags = entity.get("tags", [])
            tags_yaml = "[" + ", ".join(tags) + "]" if tags else "[]"

            note = f"""---
type: entity
entity_type: {entity.get('entity_type', 'concept')}
name: {name}
importance: {entity.get('importance', 0.5):.1f}
tags: {tags_yaml}
created: {today}
updated: {today}
---

{entity.get('description', '')}
"""
            filepath.write_text(note, encoding="utf-8")
            stats["entities_created"] += 1
            logger.info("Entity created: %s", filepath.name)
        except Exception as e:
            logger.warning("Failed to create entity '%s': %s", entity.get("name", "?"), e)
            stats["errors"] += 1

    # Append updates to existing facts
    for update in synthesis_result.get("updated_facts", []):
        try:
            title = update.get("title", "").strip()
            update_text = update.get("update", "").strip()
            if not title or not update_text:
                continue

            facts_dir = vault_root / "Memory" / "Facts"
            if not facts_dir.exists():
                continue

            # Find the fact by slug match
            slug = _slugify(title)
            candidates = list(facts_dir.glob(f"*{slug[:30]}*.md"))

            if not candidates:
                # Try looser match
                candidates = [f for f in facts_dir.glob("*.md") if slug[:20] in _slugify(f.stem)]

            if not candidates:
                logger.debug("No existing fact found for update: %s", title)
                continue

            # Append to the most recent match
            target = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]
            existing = target.read_text(encoding="utf-8")

            # Update the 'updated' date in frontmatter
            updated_content = re.sub(
                r"updated: \d{4}-\d{2}-\d{2}",
                f"updated: {today}",
                existing,
            )

            # Append the update
            appendix = f"\n\n## Update ({today})\n\n{update_text}"
            updated_content += appendix

            target.write_text(updated_content, encoding="utf-8")
            stats["facts_updated"] += 1
            logger.info("Fact updated: %s", target.name)
        except Exception as e:
            logger.warning("Failed to update fact '%s': %s", update.get("title", "?"), e)
            stats["errors"] += 1

    logger.info("Synthesis applied: %s", stats)
    return stats


# ---------------------------------------------------------------------------
# 4. Synthesize Recall — Query-time LLM synthesis
# ---------------------------------------------------------------------------

RECALL_SYNTHESIS_PROMPT = """\
You are helping an AI agent recall information from its persistent memory.

QUERY: {query}

SEARCH RESULTS:
{results_text}

Synthesize a clear, concise answer from these results. Focus on:
1. Directly answering the query
2. Including specific details (dates, names, decisions)
3. Noting any contradictions or changes over time
4. Flagging if the information seems incomplete

Keep it under 200 words. Be factual — don't speculate beyond what the results show.
"""


def synthesize_recall(
    query: str,
    search_results: list,
    llm_call_fn: Callable[[str], str],
) -> str:
    """LLM-powered synthesis over search results for enhanced recall.

    Args:
        query: The user's original search query.
        search_results: List of dicts from FTS5 search (path, title, excerpt, etc.).
        llm_call_fn: Callable that takes a prompt string, returns LLM response string.

    Returns:
        Synthesized answer string.
    """
    if not search_results:
        return ""

    results_text = ""
    for i, r in enumerate(search_results[:10], 1):
        title = r.get("title", "Unknown")
        rtype = r.get("type") or r.get("subtype", "note")
        excerpt = r.get("excerpt") or r.get("fact", "")
        results_text += f"\n{i}. [{rtype}] {title}\n   {excerpt}\n"

    prompt = RECALL_SYNTHESIS_PROMPT.format(query=query, results_text=results_text)

    try:
        synthesis = llm_call_fn(prompt)
        return synthesis.strip()
    except Exception as e:
        logger.warning("Recall synthesis failed: %s", e)
        # Return raw results as fallback
        return "\n".join(
            f"- {r.get('title', '?')}: {r.get('excerpt', r.get('fact', ''))}"
            for r in search_results[:5]
        )


# ---------------------------------------------------------------------------
# Convenience: Full Dream Pass
# ---------------------------------------------------------------------------

def run_dream(
    vault_root: str,
    llm_call_fn: Callable[[str], str],
    days: int = 1,
    reindex_fn: Optional[Callable] = None,
) -> dict:
    """Run a complete dream cycle: synthesize → apply → reindex.

    Args:
        vault_root: Path to Obsidian vault.
        llm_call_fn: Callable for LLM synthesis calls.
        days: Days of raw sessions to process.
        reindex_fn: Optional callable to re-index the vault after writes.

    Returns:
        Combined dict with synthesis results and apply stats.
    """
    synthesis = synthesize_sessions(vault_root, llm_call_fn, days=days)
    apply_stats = apply_synthesis(vault_root, synthesis)

    result = {
        **synthesis,
        **apply_stats,
        "dream_completed": datetime.now().isoformat(),
    }

    # Re-index if function provided
    if reindex_fn and (apply_stats["facts_created"] > 0 or apply_stats["entities_created"] > 0):
        try:
            reindex_fn()
            result["reindexed"] = True
        except Exception as e:
            logger.warning("Re-index after dream failed: %s", e)
            result["reindexed"] = False
            result["reindex_error"] = str(e)

    return result
