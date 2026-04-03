"""
Session save — writes a lightweight session snapshot to the vault.
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engram.session")


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:40]


def save_session(
    vault_root: str,
    summary: str,
    open_threads: list = None,
    entities_referenced: list = None,
    platform: str = "cli",
    title: Optional[str] = None,
) -> str:
    """Write a session snapshot to Memory/Sessions/. Returns the file path."""

    vault_root = Path(vault_root).expanduser()
    sessions_dir = vault_root / "Memory" / "Sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.utcnow()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M")

    note_title = title or f"Session {date_str} {time_str}"
    slug = f"{date_str}-{time_str}"
    filename = f"{slug}.md"
    filepath = sessions_dir / filename

    open_threads = open_threads or []
    entities_referenced = entities_referenced or []

    # Build frontmatter
    threads_yaml = "\n".join(f"  - {t}" for t in open_threads) if open_threads else "  []"
    entities_yaml = "\n".join(
        f"  - '[[{e}]]'" for e in entities_referenced
    ) if entities_referenced else "  []"

    content = f"""---
type: session
title: {note_title}
platform: {platform}
status: closed
open_threads:
{threads_yaml}
entities_referenced:
{entities_yaml}
created: {date_str}
updated: {date_str}
---

{summary}
"""

    filepath.write_text(content, encoding="utf-8")
    logger.info(f"Session saved: {filepath}")
    return str(filepath)
