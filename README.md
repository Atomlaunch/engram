# Engram

**Temporal Knowledge Graph Memory for AI Agents**

Engram is a self-hosted memory system that gives AI agents persistent memory across sessions. It ingests conversation transcripts, extracts entities, facts, relationships, and emotions using an LLM, and stores them in an embedded graph database ([Kùzu](https://kuzudb.com/)).

Built for [OpenClaw](https://github.com/openclaw/openclaw) but designed to work with any agent framework that produces conversation logs.

## What it does

- **Extracts knowledge** from conversations — entities, facts, relationships, emotions
- **Builds a graph** — 2,800+ nodes and 8,100+ relationships from real usage
- **Provides context** — agents wake up knowing what happened yesterday
- **Runs locally** — no cloud dependencies, embedded database, your data stays yours
- **Uses any LLM** — xAI (Grok) by default, or any OpenAI-compatible endpoint

## Quick Start

```bash
# Install
python3 -m venv .venv
source .venv/bin/activate
pip install kuzu mcp

# Configure
# Edit config.json — set your preferred model

# Run
python export_sessions.py          # Export sessions to markdown
python engram.py ingest            # Extract knowledge into graph
python engram.py briefing          # Generate agent briefing
python engram.py search "query"    # Search the knowledge graph
```

See [SKILL.md](SKILL.md) for the complete setup guide.

## Architecture

```
Sessions (JSONL) → export → Markdown → ingest → LLM extraction → Kùzu Graph DB → API/CLI
```

## API

HTTP server on port 3456:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Node/relationship counts |
| `/stats` | GET | Breakdown by type |
| `/briefing` | GET | Full session briefing |
| `/search` | POST | Semantic search |
| `/entity` | POST | Entity deep context |
| `/recent` | POST | Recent episodes |

## LLM Backend

Engram uses xAI (Grok) for entity and relationship extraction.

**API key resolution** (in priority order):
1. `XAI_API_KEY` env var
2. `xai_api_key` in `config.json`
3. `skills.entries.grok.apiKey` in `~/.openclaw/openclaw.json` (auto-detected if you use OpenClaw)

Override the model via `ENGRAM_MODEL` env var or `model` in `config.json` (default: `grok-3-mini-fast`).

## Requirements

- Python 3.10+
- xAI API key (get one at [x.ai](https://x.ai))
- ~50MB disk for the graph database

## License

MIT
