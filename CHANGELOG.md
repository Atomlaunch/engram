# Changelog

All notable changes to Engram are documented here.
Follows [Keep a Changelog](https://keepachangelog.com) and [Semantic Versioning](https://semver.org).

---

## [1.0.0] - 2026-04-02

### Added
- Obsidian-native architecture -- vault is the source of truth
- SQLite FTS5 index with incremental mtime-based updates
- File watcher (watchdog) with inotify on Linux, polling fallback for WSL2 /mnt/ paths
- 3 note types: Entity, Fact, Session
- 7 artifact types for Facts: durable_fact, preference, standing_rule, decision, lesson, open_loop, constraint
- Wikilink graph indexed in SQLite links table
- Ingest pipeline: LLM extraction -> vault write-back (Entity + Fact notes)
- Briefing generator: standing rules + open loops + recent session + top entities + recent facts
- Session save to vault (Memory/Sessions/)
- Query API: search_facts, search_entities, get_standing_rules, get_open_loops, get_sessions, get_facts_about, full_text_search
- CLI: engram index | brief | search | ingest | session | version
- Vault templates: entity.md, fact.md, session.md, AGENTS.md
- pyproject.toml package structure, pip installable

### Removed
- Neo4j / Kuzu graph database dependency
- Docker requirement
- Episode nodes (raw conversation storage)
- Emotion nodes
- Multi-agent scoping (agent_id fields)
- HTTP server / dashboard
- MCP server (planned for v1.1)

### Notes
- v0 legacy code preserved at git tag v0-legacy
- Requires Python >= 3.11
- Config at ~/.engram/config.json
- Index DB at ~/.engram/vault_index.db

---

## [0.x] - Legacy (OpenClaw)

Neo4j-based system. See git tag v0-legacy.
37k nodes, 121k relationships, multi-agent, Docker-dependent.
Replaced by this architecture.
