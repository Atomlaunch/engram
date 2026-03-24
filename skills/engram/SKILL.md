---
name: engram
description: Set up and manage Engram — a graph-based memory system for OpenClaw agents. Use when installing Engram for the first time, configuring multi-agent memory, running ingest/export, managing the dashboard, or troubleshooting memory issues.
---

# Engram — Graph Memory for OpenClaw

Engram gives OpenClaw agents persistent, structured memory via a Kuzu graph database. It extracts entities, facts, relationships, and emotions from session logs and memory files, stores them in a queryable graph, and injects relevant context into every conversation turn via the context engine plugin.

## Architecture

```
Session logs → Export → Markdown → LLM Extraction → Kuzu Graph DB
                                                        ↓
                                        Context Engine Plugin → Agent turns
                                                        ↓
                                            Dashboard (optional)
```

**Components:**
- `engram/` — Core: ingest, query, schema, export, dedup, consolidation
- `dashboard/` — FastAPI + Sigma.js visualization (optional)
- `extensions/context-engine/` — OpenClaw plugin for context injection

## First-Time Setup

### 1. Clone and install

```bash
cd <your-openclaw-workspace>
git clone https://github.com/Atomlaunch/engram.git engram
```

### 2. Python environment

```bash
python3 -m venv .venv-memory
source .venv-memory/bin/activate
pip install kuzu chromadb
```

### 3. Configure

Copy the example config and fill in your values:

```bash
cd engram
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "model": "grok-3-mini-fast",
  "xai_api_key": "your-xai-api-key",

  "main_agent_id": "main",
  "memory_dir": "~/your-workspace/memory",

  "agent_workspaces": {
    "agent-two": "~/.openclaw/workspace-agent-two/memory"
  },

  "ingest_workers": 6,

  "context_engine": {
    "workspace_root": "/full/path/to/your/workspace",
    "engram_dir": "/full/path/to/your/workspace/engram",
    "python_bin": "/full/path/to/your/workspace/.venv-memory/bin/python",
    "agents_dir": "~/.openclaw/agents"
  }
}
```

**Key fields:**
- `main_agent_id` — Your primary agent's ID. All files in `memory_dir` default to this.
- `memory_dir` — Where your main agent's memory files live.
- `agent_workspaces` — Map of additional agent IDs to their memory directories.
- `xai_api_key` — For LLM extraction. Also reads from `XAI_API_KEY` env or OpenClaw's `skills.entries.grok.apiKey`.
- `context_engine` — Paths for the OpenClaw plugin. Leave empty to auto-detect.

### 4. Register the context engine plugin

Add to your OpenClaw config (`~/.openclaw/openclaw.json`):

```json
{
  "plugins": {
    "allow": ["engram-context-engine"],
    "load": {
      "paths": ["/path/to/engram/extensions/context-engine"]
    },
    "slots": {
      "contextEngine": "engram-context-engine"
    },
    "entries": {
      "engram-context-engine": { "enabled": true }
    },
    "installs": {
      "engram-context-engine": {
        "source": "path",
        "sourcePath": "/path/to/engram/extensions/context-engine",
        "installPath": "/path/to/engram/extensions/context-engine",
        "version": "0.1.0"
      }
    }
  }
}
```

### 5. Initial ingest

```bash
# Export existing sessions to markdown
.venv-memory/bin/python engram/export_sessions.py

# Run ingest (parallel)
.venv-memory/bin/python engram/ingest.py --workers 6
```

### 6. Set up cron (hourly)

```bash
crontab -e
# Add (adjust paths):
0 */1 * * * cd /path/to/workspace && .venv-memory/bin/python engram/export_sessions.py >> /tmp/engram-export.log 2>&1 && .venv-memory/bin/python engram/engram.py ingest >> /tmp/engram-ingest.log 2>&1
```

### 7. Dashboard (optional)

```bash
cd engram/dashboard
npm install && node bundle-deps.js
pm2 start ecosystem.config.js
# → http://localhost:3847
```

**Note:** Kuzu allows only one writer. Stop the dashboard before running ingest cron.

## Multi-Agent Memory

Each agent's facts are scoped by `agent_id`. Queries return facts matching `agent_id = '<agent>' OR agent_id = 'shared'`.

Configure agents in `config.json`:
- `main_agent_id` — files in `memory_dir` default to this
- `agent_workspaces` — additional agents mapped to their memory directories

Agent resolution order (in `extract_agent_from_filepath()`):
1. File in an `agent_workspaces` directory → that agent's ID
2. File in `memory_dir` with filename pattern `YYYY-MM-DD-<agent>-<hash>.md` matching a configured agent → that agent
3. Any other file in `memory_dir` → `main_agent_id`
4. Unknown location → `shared`

## Key Commands

```bash
# Parallel ingest
.venv-memory/bin/python engram/ingest.py --workers 6

# Force re-ingest all
.venv-memory/bin/python engram/ingest.py --force --workers 6

# Query memories
.venv-memory/bin/python engram/context_query.py query "search terms" --agent main

# Entity deduplication
.venv-memory/bin/python engram/dedup_entities.py --dry-run
.venv-memory/bin/python engram/dedup_entities.py --execute

# Stats
.venv-memory/bin/python engram/engram.py stats

# Dream consolidation (nightly)
.venv-memory/bin/python engram/engram.py dream
```

## Troubleshooting

| Issue | Fix |
|---|---|
| DB lock error | Stop dashboard (`pm2 delete engram-dashboard`), wait 5s |
| Query returns 0 | Terms <3 chars are skipped. Use specific terms. |
| Cross-agent bleed | Check `config.json` — ensure `memory_dir` maps to `main_agent_id`, not `shared` |
| Slow ingest | Use `--workers 6` for parallel extraction |
| Dashboard wrong counts | Run dedup, verify `agent_id` on nodes |
