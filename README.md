# Engram

Jarvis's persistent memory system. Obsidian-native, SQLite-indexed, zero heavy dependencies.

## Architecture

- **Source of truth**: Obsidian vault (markdown files with YAML frontmatter)
- **Query layer**: SQLite FTS5 index, rebuilt incrementally on change
- **Live sync**: watchdog file watcher (inotify on Linux)
- **Ingest**: LLM extraction pipeline writes Entity + Fact notes back to vault
- **Briefing**: session start injection with standing rules, open loops, recent context

## Install

```bash
git clone https://github.com/atomlaunch/engram.git
cd engram
pip install -e .
```

## Setup

```bash
# Copy config template
mkdir -p ~/.engram
cp config.example.json ~/.engram/config.json
# Edit vault_path and other settings

# Copy AGENTS.md to vault root
cp templates/AGENTS.md ~/obsidian-vault/AGENTS.md

# Build initial index
engram index
```

## Usage

```bash
engram index              # Scan vault and build index
engram index --watch      # Keep running and watch for changes
engram brief              # Print session briefing
engram search "discord"   # Full-text search
engram search "rule" --type fact
engram ingest             # Run LLM extraction pipeline
engram session "Summary of what happened" --threads "item1,item2"
engram version            # Version + index stats
```

## Note Types

- **entity** -- people, projects, tools, concepts (Memory/Entities/)
- **fact** -- decisions, preferences, rules, lessons (Memory/Facts/)
- **session** -- session snapshots (Memory/Sessions/)

## Artifact Types (Facts)

| Type | Description |
|------|-------------|
| durable_fact | Stable truth, long shelf life |
| preference | How TheDev likes things done |
| standing_rule | Always apply (importance: 1.0) |
| decision | Something decided with context |
| lesson | Learned from failure or success |
| open_loop | Unresolved thread to revisit |
| constraint | Hard limit or boundary |

## Versioning

Semver. Each release tagged on GitHub. Schema migrations in `migrations/`.

See [CHANGELOG.md](CHANGELOG.md) for full history.

## Requirements

- Python >= 3.11
- watchdog >= 4.0
- python-frontmatter >= 1.1
- anthropic >= 0.40 (for ingest pipeline)
- click >= 8.1
