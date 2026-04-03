"""
Briefing generator — produces a lean, token-budgeted markdown briefing
injected at session start.

Design principles:
- Hard char budget (default 800). Fits in ~200 tokens.
- Standing rules first, always. But skip anything already in SOUL.md.
- Context-aware: filter by platform (cli vs discord) if known.
- Truncate aggressively rather than blow the budget.
"""

import logging
import re
from pathlib import Path
from typing import Optional

import frontmatter as fm

from .query import (
    get_standing_rules,
    get_open_loops,
    get_sessions,
    get_top_entities,
    get_recent_facts,
)

logger = logging.getLogger("engram.briefing")

# Hard budget in characters. ~200 tokens at avg 4 chars/token.
DEFAULT_BUDGET = 800

# Keywords that indicate a fact is already covered in SOUL.md / system prompt.
# If a fact's content matches enough of these, skip it.
_SOUL_COVERED = [
    # Compartmentalization -- in SOUL.md
    {"compartment", "guild", "silo", "never share"},
    # Tier 3 -- in SOUL.md trust tiers section
    {"tier", "flag", "wait", "approval", "irreversible", "external"},
    # Permission prompts -- in SOUL.md
    {"permission", "prompt", "micromanage", "just do"},
    # Discord components -- in SOUL.md standing rule (we keep this one -- it's behavioral)
]

# Tokens that are pure noise in a briefing line
_NOISE_PATTERN = re.compile(
    r"\b(the|a|an|is|are|was|were|be|been|and|or|but|in|on|at|to|for|of|with|this|that|it|its)\b",
    re.IGNORECASE
)


def _soul_covers(content: str, soul_words: set, threshold: float = 0.7) -> bool:
    """
    Return True if content is already well-covered by SOUL.md.
    Uses direct word overlap ratio between content and SOUL text.
    """
    if not soul_words or not content:
        return False
    content_words = set(re.findall(r"\w+", content.lower()))
    # Only count meaningful words (len > 3, not stopwords)
    stopwords = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                 "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
                 "this", "that", "it", "its", "not", "no", "will", "can", "do"}
    meaningful = {w for w in content_words if len(w) > 3 and w not in stopwords}
    if len(meaningful) < 3:
        return False
    overlap = len(meaningful & soul_words) / len(meaningful)
    return overlap >= threshold


def _load_soul_text(soul_path: Optional[str] = None) -> str:
    """Load SOUL.md content for dedup filtering."""
    candidates = [
        soul_path,
        str(Path("~/.hermes/SOUL.md").expanduser()),
    ]
    for path in candidates:
        if path and Path(path).expanduser().exists():
            try:
                return Path(path).expanduser().read_text()
            except Exception:
                pass
    return ""


def _read_note_body(vault_root: str, rel_path: str) -> str:
    path = Path(vault_root).expanduser() / rel_path
    try:
        post = fm.load(str(path))
        return post.content.strip()
    except Exception:
        return ""


def _truncate(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    return text[:budget - 3] + "..."


def generate_briefing(
    conn,
    vault_root: str,
    max_facts: int = 10,
    max_entities: int = 8,
    budget: int = DEFAULT_BUDGET,
    platform: Optional[str] = None,
    soul_path: Optional[str] = None,
) -> str:
    """
    Generate a lean session briefing within a hard char budget.

    Priority order (fill until budget exhausted):
      1. Standing rules (skip SOUL.md dupes, skip platform-irrelevant)
      2. Open loops (max 2)
      3. Last session open_threads only (not full summary)
      4. Key entities (names only, no descriptions)
      5. Recent facts (titles only, no excerpts)
    """
    soul_text = _load_soul_text(soul_path)
    soul_words = set(re.findall(r"\w+", soul_text.lower()))

    remaining = budget
    sections = []

    # --- 1. Standing rules ---
    rules = get_standing_rules(conn)
    rule_lines = []
    for r in rules:
        body = _read_note_body(vault_root, r["path"])
        statement = body.split("\n")[0].strip() if body else r["title"]

        # Skip if already covered in SOUL.md (high word overlap)
        statement_words = set(re.findall(r"\w+", statement.lower()))
        if soul_words and len(statement_words) > 5:
            overlap = len(statement_words & soul_words) / len(statement_words)
            if overlap > 0.6:
                logger.debug(f"Skipping rule (SOUL covered): {statement[:50]}")
                continue

        # Skip platform-irrelevant rules
        if platform == "cli" and "discord" in statement.lower():
            logger.debug(f"Skipping discord rule for CLI session")
            continue

        line = f"- {statement}"
        rule_lines.append(line)

    if rule_lines:
        header = "## Rules\n"
        block = header + "\n".join(rule_lines) + "\n"
        if len(block) <= remaining:
            sections.append(block)
            remaining -= len(block)
        else:
            # Fit as many rules as possible
            fitted = header
            for line in rule_lines:
                if len(fitted) + len(line) + 1 <= remaining:
                    fitted += line + "\n"
            if fitted != header:
                sections.append(fitted)
                remaining -= len(fitted)

    if remaining <= 50:
        return "\n".join(sections).strip()

    # --- 2. Open loops (max 2, title only) ---
    loops = get_open_loops(conn, limit=2)
    if loops:
        # Clean up slug-style titles to readable text
        def _clean_title(t):
            # Strip date prefix (2026-04-03-)
            t = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", t)
            return t.replace("-", " ")
        lines = [f"- {_clean_title(l['title'])}" for l in loops]
        block = "## Open\n" + "\n".join(lines) + "\n"
        if len(block) <= remaining:
            sections.append(block)
            remaining -= len(block)

    if remaining <= 50:
        return "\n".join(sections).strip()

    # --- 3. Last session -- open threads only, not full summary ---
    sessions = get_sessions(conn, limit=1)
    if sessions:
        s = sessions[0]
        body = _read_note_body(vault_root, s["path"])
        # Extract open_threads from frontmatter
        try:
            post = fm.load(str(Path(vault_root).expanduser() / s["path"]))
            threads = post.metadata.get("open_threads", [])
            if threads:
                thread_lines = "\n".join(f"  - {t}" for t in threads[:3])
                block = f"## Last session threads\n{thread_lines}\n"
                if len(block) <= remaining:
                    sections.append(block)
                    remaining -= len(block)
        except Exception:
            pass

    if remaining <= 50:
        return "\n".join(sections).strip()

    # --- 4. Key entities (names only) ---
    entities = get_top_entities(conn, limit=max_entities)
    if entities:
        names = ", ".join(e["title"] for e in entities)
        block = f"## Entities\n{names}\n"
        if len(block) <= remaining:
            sections.append(block)
            remaining -= len(block)
        else:
            # Truncate entity list to fit
            truncated = _truncate(names, remaining - 12)
            block = f"## Entities\n{truncated}\n"
            sections.append(block)
            remaining -= len(block)

    if remaining <= 50:
        return "\n".join(sections).strip()

    # --- 5. Recent facts (titles only, skip SOUL-covered) ---
    facts = get_recent_facts(conn, days=7, limit=max_facts)
    def _clean_slug(t):
        t = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", t)
        return t.replace("-", " ").strip()

    fact_lines = []
    for f in facts:
        # Skip standing rules -- already shown above
        if f.get("subtype") == "standing_rule":
            continue
        # Skip anything well-covered by SOUL.md
        body = _read_note_body(vault_root, f["path"])
        fact_statement = body.split("\n")[0].strip() if body else ""
        if fact_statement and _soul_covers(fact_statement, soul_words):
            continue
        title = _clean_slug(f["title"])
        line = f"- [{f.get('subtype', 'fact')}] {title}"
        fact_lines.append(line)

    if fact_lines:
        block = "## Recent\n" + "\n".join(fact_lines) + "\n"
        if len(block) <= remaining:
            sections.append(block)
        else:
            fitted = "## Recent\n"
            for line in fact_lines:
                if len(fitted) + len(line) + 1 <= remaining:
                    fitted += line + "\n"
                else:
                    break
            if fitted != "## Recent\n":
                sections.append(fitted)

    result = "\n".join(sections).strip()
    logger.debug(f"Briefing: {len(result)} chars (budget: {budget})")
    return result
