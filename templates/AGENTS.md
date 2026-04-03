# AGENTS.md — Engram Vault Schema

This file is Jarvis's operating manual for this vault.
Always read this before reading or writing any memory notes.

---

## Vault Structure

  Memory/
    Entities/   -- People, projects, tools, concepts worth knowing long-term
    Facts/      -- Things I know: decisions, preferences, rules, lessons, open loops
    Sessions/   -- Lightweight session snapshots
  Daily/        -- TheDev's notes (read by ingest pipeline, not written by Jarvis)
  Projects/     -- Project context (read by ingest pipeline)

---

## Note Types

### entity
Long-lived things worth knowing about.

Required frontmatter:
  type: entity
  entity_type: person | project | tool | concept | place | org
  name: Full Name
  importance: 0.0-1.0
  created: YYYY-MM-DD
  updated: YYYY-MM-DD

Optional:
  tags: [list]

Body: description + [[wikilinks]] to related entities.

### fact
The core memory unit. One clear statement per note.

Required frontmatter:
  type: fact
  artifact_type: (see below)
  status: active | superseded | archived
  importance: 0.0-1.0
  confidence: 0.0-1.0
  source: ingest | live | manual
  created: YYYY-MM-DD
  updated: YYYY-MM-DD

Optional:
  about: ['[[Entity Name]]']       -- entities this fact is about
  superseded_by: '[[fact-slug]]'   -- if status: superseded
  tags: [list]

Body: the fact itself in one sentence. Context/reasoning below if needed.

### session
Lightweight snapshot. Captures what happened and what's unresolved.

Required frontmatter:
  type: session
  platform: cli | discord
  status: closed
  created: YYYY-MM-DD
  updated: YYYY-MM-DD

Optional:
  open_threads: [list]
  entities_referenced: ['[[Name]]']

Body: 2-4 sentence summary.

---

## Artifact Types (for Facts)

  durable_fact   -- stable truth, long shelf life
  preference     -- how TheDev likes things done
  standing_rule  -- ALWAYS apply (highest priority, importance: 1.0)
  decision       -- something decided, with context
  lesson         -- learned from failure or success
  open_loop      -- unresolved thread to revisit
  constraint     -- hard limit or boundary

---

## Naming Conventions

Entities:   Memory/Entities/<name-slugified>.md
Facts:      Memory/Facts/<YYYY-MM-DD>-<title-slugified>.md
Sessions:   Memory/Sessions/<YYYY-MM-DD>-<HHMM>.md

---

## What Makes a Good Fact

GOOD:
  - "TheDev prefers inline Discord components over plain text bullet points for choices"
  - "SkillBoard runs on Hermes at port 3001, managed by pm2"
  - "All agent writes to external services require TheDev confirmation (Tier 3 action)"

BAD (don't store):
  - "Working on the engram spec" (temporary status)
  - "Claude is an AI" (obvious)
  - "Today's date is 2026-04-02" (ephemeral)

---

## Standing Rules

Standing rules have artifact_type: standing_rule and importance: 1.0.
They are ALWAYS injected at session start regardless of recency.
Treat them as hard rules, not suggestions.
