#!/usr/bin/env python3
"""
engram.cleanup_sessions
-----------------------
Prune stale ephemeral sessions from OpenClaw's sessions.json.

If you use llm_mode: "openclaw" in Engram's config, each LLM call creates a
session entry in ~/.openclaw/agents/main/sessions/sessions.json. Over time this
file can grow to 100MB+, causing 7-8 second delays on every inbound message as
OpenClaw parses the entire file per request.

Run this periodically (or add to cron) if you use openclaw mode:

    python -m engram.cleanup_sessions
    python -m engram.cleanup_sessions --dry-run
    python -m engram.cleanup_sessions --agent sillyfarms
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


def cleanup_sessions(
    agent: str = "main",
    dry_run: bool = False,
    max_age_days: int = 7,
    verbose: bool = True,
) -> dict:
    """
    Remove stale session entries from OpenClaw's sessions.json.

    Args:
        agent: Agent ID whose sessions.json to clean (default: "main")
        dry_run: If True, report what would be removed without writing
        max_age_days: Remove cron sessions older than this many days
        verbose: Print progress

    Returns:
        dict with before/after counts and file sizes
    """
    sessions_path = Path.home() / ".openclaw" / "agents" / agent / "sessions" / "sessions.json"

    if not sessions_path.exists():
        raise FileNotFoundError(f"sessions.json not found: {sessions_path}")

    size_before = sessions_path.stat().st_size

    with open(sessions_path) as f:
        data: dict = json.load(f)

    count_before = len(data)
    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).timestamp() * 1000

    removed = []
    kept = {}

    for key, value in data.items():
        parts = key.split(":")
        session_type = parts[2] if len(parts) >= 3 else ""

        # Always remove: openai embedding/api sessions (created by Engram openclaw mode)
        if session_type == "openai":
            removed.append((key, "stale openai/api session"))
            continue

        # Remove old cron sessions beyond max_age_days
        if session_type == "cron":
            updated_at = value.get("updatedAt", 0)
            if updated_at < cutoff_ms:
                removed.append((key, f"cron session older than {max_age_days}d"))
                continue

        kept[key] = value

    count_after = len(kept)
    pruned = count_before - count_after

    if verbose:
        print(f"sessions.json: {count_before} entries ({size_before / 1024 / 1024:.1f} MB)")
        print(f"  Pruning {pruned} entries ({len([r for r in removed if 'openai' in r[1]])} openai, "
              f"{len([r for r in removed if 'cron' in r[1]])} old cron)")

    if not dry_run and pruned > 0:
        backup_path = sessions_path.with_suffix(".json.bak")
        shutil.copy2(sessions_path, backup_path)
        with open(sessions_path, "w") as f:
            json.dump(kept, f)
        size_after = sessions_path.stat().st_size
        if verbose:
            print(f"  After: {count_after} entries ({size_after / 1024 / 1024:.1f} MB)")
            print(f"  Backup saved to: {backup_path}")
    elif dry_run:
        size_after = size_before
        if verbose:
            print("  (dry-run — no changes written)")
    else:
        size_after = size_before
        if verbose:
            print("  Nothing to prune.")

    return {
        "before": {"count": count_before, "bytes": size_before},
        "after": {"count": count_after, "bytes": size_after},
        "pruned": pruned,
        "dry_run": dry_run,
    }


def main():
    parser = argparse.ArgumentParser(description="Clean up stale OpenClaw sessions created by Engram")
    parser.add_argument("--agent", default="main", help="Agent ID (default: main)")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument("--max-age-days", type=int, default=7, help="Max age for cron sessions (default: 7)")
    args = parser.parse_args()

    try:
        result = cleanup_sessions(agent=args.agent, dry_run=args.dry_run, max_age_days=args.max_age_days)
        if result["pruned"] > 0 and not args.dry_run:
            print("Done.")
        sys.exit(0)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
