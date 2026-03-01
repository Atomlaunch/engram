# Engram — Temporal Knowledge Graph Memory

Engram is a self-hosted memory system for OpenClaw agents. It ingests session transcripts, extracts entities/facts/relationships/emotions using an LLM, and stores them in an embedded graph database (Kùzu). Agents query it for context, history, and relationship mapping.

**What it gives you:** Persistent memory across sessions. Your agent wakes up knowing what happened yesterday, who it talked to, what decisions were made, and how the user felt about it.

---

## Quick Start

### 1. Prerequisites

- **Python 3.10+**
- **OpenClaw** running with at least one LLM provider configured (any model works — Grok, Haiku, Sonnet, Ollama, etc.)
- **OpenClaw chat completions endpoint enabled** — check your `~/.openclaw/openclaw.json`:
  ```json
  {
    "gateway": {
      "http": {
        "endpoints": {
          "chatCompletions": { "enabled": true }
        }
      }
    }
  }
  ```

### 2. Install

```bash
# Clone or copy the engram/ directory into your workspace
# Example: ~/clawd/engram/

# Create a Python virtual environment
cd ~/clawd
python3 -m venv .venv-memory
source .venv-memory/bin/activate

# Install dependencies
pip install kuzu mcp
```

### 3. Initialize the database

```bash
cd ~/clawd
source .venv-memory/bin/activate
python engram/engram.py search "test"
# This creates .engram-db/ on first run
```

### 4. Configure

Edit `engram/config.json`:

```json
{
  "model": "grok",
  "llm_mode": "openclaw",
  "ingest_interval_minutes": 60,
  "max_concurrent_chunks": 5,
  "openclaw_port": null,
  "openclaw_token": null,
  "sessions_dir": "~/.openclaw/agents",
  "memory_dir": "~/clawd/memory"
}
```

| Field | What it does | Default |
|-------|-------------|---------|
| `model` | OpenClaw model alias for extraction | `grok` |
| `llm_mode` | `openclaw` (gateway) or `xai` (direct API) | `openclaw` |
| `ingest_interval_minutes` | How often the cron runs | `60` |
| `max_concurrent_chunks` | Parallel API calls per file | `5` |
| `openclaw_port` | Gateway port (`null` = auto-detect) | `null` |
| `openclaw_token` | Gateway auth token (`null` = auto-detect) | `null` |
| `sessions_dir` | Where OpenClaw stores session JSONL files | `~/.openclaw/agents` |
| `memory_dir` | Where exported markdown files go | `~/clawd/memory` |

Port and token are auto-detected from `~/.openclaw/openclaw.json` if left as `null`.

The `model` field accepts any alias your OpenClaw instance recognizes: `grok`, `haiku`, `sonnet`, `opus`, `qwen-local`, etc.

### 5. Run the pipeline manually (first time)

```bash
cd ~/clawd
source .venv-memory/bin/activate

# Step 1: Export OpenClaw sessions to markdown
python engram/export_sessions.py

# Step 2: Ingest markdown into the knowledge graph
python engram/engram.py ingest

# Step 3: Generate a briefing
python engram/engram.py briefing > BRIEFING.md
```

**Note:** The first full ingest takes a while — each file is chunked and sent to the LLM for entity extraction. A workspace with 70+ sessions takes ~30-60 minutes serially. Subsequent runs only process new files.

### 6. Set up the cron (automated)

```bash
bash engram/update-cron.sh
```

This installs a cron job that runs the full pipeline (export → ingest → briefing) at the interval specified in `config.json`. Default: every hour.

To change the interval, edit `config.json` and re-run `update-cron.sh`.

### 7. Start the HTTP API server

```bash
# Using pm2 (recommended):
pm2 start .venv-memory/bin/python --name engram-http -- engram/http_server.py
pm2 save

# Or run directly:
source .venv-memory/bin/activate
python engram/http_server.py
```

The server runs on port **3456** by default. Set `ENGRAM_PORT` env var to change it.

---

## Usage

### CLI

```bash
cd ~/clawd && source .venv-memory/bin/activate

# Search the knowledge graph
python engram/engram.py search "dashboard voice chat"

# Get a full briefing
python engram/engram.py briefing

# Ingest new files
python engram/engram.py ingest

# Run overnight consolidation (merges duplicate entities, strengthens patterns)
python engram/engram.py dream
```

### HTTP API

All endpoints are on `http://localhost:3456` (or your configured port).

#### `GET /health`
```json
{"ok": true, "nodes": 2846, "relationships": 8116}
```

#### `GET /stats`
Returns counts by node and relationship type.

#### `GET /briefing`
Full session briefing with recent activity, key entities, knowledge, and emotional context. Use this to give your agent startup context.

#### `POST /search`
```bash
curl -X POST http://localhost:3456/search \
  -H "Content-Type: application/json" \
  -d '{"query": "dashboard", "limit": 5}'
```
Returns matching entities, facts, and episodes.

#### `POST /entity`
```bash
curl -X POST http://localhost:3456/entity \
  -H "Content-Type: application/json" \
  -d '{"name": "Jarvis"}'
```
Returns the entity with all relationships, facts, and episodes.

#### `POST /recent`
```bash
curl -X POST http://localhost:3456/recent \
  -H "Content-Type: application/json" \
  -d '{"hours": 24, "limit": 10}'
```
Returns recent episodes within the time window.

---

## How it works

### The Pipeline

```
OpenClaw Sessions (JSONL)
        │
        ▼
  export_sessions.py    ← Converts JSONL → clean markdown
        │
        ▼
  ~/clawd/memory/*.md   ← One file per session
        │
        ▼
  ingest.py             ← Chunks text, calls LLM for extraction
        │
        ▼
  LLM (via OpenClaw)    ← Extracts entities, facts, relationships, emotions
        │
        ▼
  Kùzu Graph DB         ← Stores everything with timestamps + confidence
        │
        ▼
  HTTP API / CLI         ← Query the graph
```

### What gets extracted

For each chunk of conversation, the LLM produces:

- **Entities**: People, tools, projects, concepts with types and descriptions
- **Facts**: Knowledge statements with confidence scores and timestamps
- **Relationships**: How entities connect — `uses`, `created`, `part_of`, `relates_to`, `caused`, etc.
- **Emotions**: Sentiment with valence/arousal scores and context

### Graph structure

**Nodes:**
| Type | Description |
|------|------------|
| Entity | People, tools, projects, concepts |
| Fact | Extracted knowledge with confidence scores |
| Episode | Session summaries |
| Emotion | Emotional states captured during sessions |
| SessionState | Session metadata |

**Relationships:**
| Type | Meaning |
|------|---------|
| ABOUT | Fact → Entity (this fact is about this entity) |
| MENTIONED_IN | Entity → Episode (appeared in this session) |
| DERIVED_FROM | Fact → Episode (extracted from this session) |
| RELATES_TO | Entity → Entity (general connection) |
| PART_OF | Entity → Entity (hierarchy) |
| CAUSED | Entity/Event → Entity/Event (causation) |
| ENTITY_EVOKES | Entity → Emotion |
| EPISODE_EVOKES | Episode → Emotion |

---

## Using with your agent

### Load the briefing at session start

Add `BRIEFING.md` to your agent's workspace files in OpenClaw. The hourly cron keeps it fresh. Your agent wakes up with full context of recent work, key entities, and emotional state.

### Search from your agent

Your agent can call Engram directly via CLI:

```bash
cd ~/clawd && source .venv-memory/bin/activate && python engram/engram.py search "query"
```

Or via the HTTP API:

```bash
curl -s -X POST http://localhost:3456/search \
  -H "Content-Type: application/json" \
  -d '{"query": "what did we work on yesterday"}'
```

### Add to AGENTS.md

Add this to your agent's rules so it knows to check memory:

```markdown
## Memory
- **Engram** (primary): `cd ~/clawd && source .venv-memory/bin/activate && python engram/engram.py search "query"`
- Search before answering questions about prior work, preferences, or decisions.
```

---

## File reference

```
engram/
├── SKILL.md              ← This file
├── config.json           ← User configuration
├── engram.py             ← CLI entry point
├── ingest.py             ← LLM extraction pipeline
├── export_sessions.py    ← OpenClaw JSONL → markdown converter
├── http_server.py        ← REST API server
├── query.py              ← Graph query functions
├── schema.py             ← Kùzu schema definitions + migrations
├── briefing.py           ← Briefing generator
├── consolidate.py        ← Memory consolidation (dream mode)
├── run_ingest.py         ← Batch ingest runner
├── session.py            ← Session state management
├── mcp_server.py         ← MCP server (for tool-calling agents)
├── update-cron.sh        ← Apply cron schedule from config
├── requirements.txt      ← Python dependencies
└── .engram-db/           ← Kùzu graph database (created on first run)
```

---

## Environment variables (optional overrides)

| Variable | Purpose | Default |
|----------|---------|---------|
| `ENGRAM_MODEL` | Override model from config | config.json `model` |
| `ENGRAM_LLM_MODE` | Override LLM routing | config.json `llm_mode` |
| `ENGRAM_DB_PATH` | Custom database path | `engram/.engram-db` |
| `ENGRAM_PORT` | HTTP server port | `3456` |
| `XAI_API_KEY` | Required only if `llm_mode=xai` | — |

---

## Troubleshooting

**"LLM extraction failed: XAI_API_KEY not set"**
→ You're using `llm_mode: "xai"` without the env var. Switch to `llm_mode: "openclaw"` in config.json (recommended).

**"Could not set lock on file"**
→ Another process has the database open for writing. Wait for ingestion to finish, or restart the HTTP server after ingestion completes.

**"openclaw.json not found"**
→ Engram can't find your OpenClaw config. Make sure OpenClaw is installed and `~/.openclaw/openclaw.json` exists. Or set `openclaw_port` and `openclaw_token` manually in config.json.

**Ingestion is slow**
→ It's serial by default. Each chunk makes one LLM call. For 70+ sessions, expect 30-60 minutes on first run. Subsequent runs only process new files. Async parallel processing is planned.

**Empty search results**
→ Run `python engram/engram.py ingest` to make sure files have been processed. Check `/health` endpoint for node counts.
