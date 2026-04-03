"""Tests for engram.query -- search and retrieval."""

import pytest
from pathlib import Path

from engram.schema import init_db
from engram.indexer import full_index


@pytest.fixture
def populated_db(tmp_path):
    """Create a vault with known content and return (conn, vault_path)."""
    vault = tmp_path / "vault"
    (vault / "Memory" / "Facts").mkdir(parents=True)
    (vault / "Memory" / "Entities").mkdir(parents=True)
    (vault / "Memory" / "Sessions").mkdir(parents=True)

    # Standing rule
    (vault / "Memory" / "Facts" / "discord-rule.md").write_text("""---
type: fact
artifact_type: standing_rule
status: active
importance: 1.0
confidence: 1.0
tags: [discord]
created: 2026-01-01
updated: 2026-04-01
---
Always use inline Discord components for choices.
""")

    # Preference
    (vault / "Memory" / "Facts" / "thedev-pref.md").write_text("""---
type: fact
artifact_type: preference
status: active
importance: 0.8
confidence: 0.9
about:
  - '[[TheDev]]'
tags: [communication]
created: 2026-01-01
updated: 2026-04-01
---
TheDev dislikes permission prompts and prefers direct action.
""")

    # Open loop
    (vault / "Memory" / "Facts" / "open-loop.md").write_text("""---
type: fact
artifact_type: open_loop
status: active
importance: 0.7
confidence: 1.0
tags: [engram]
created: 2026-01-01
updated: 2026-04-01
---
Engram migration to Hermes is pending.
""")

    # Superseded fact
    (vault / "Memory" / "Facts" / "old-fact.md").write_text("""---
type: fact
artifact_type: durable_fact
status: superseded
importance: 0.5
confidence: 0.8
tags: []
created: 2026-01-01
updated: 2026-01-15
---
Old outdated fact that has been replaced.
""")

    # Entity
    (vault / "Memory" / "Entities" / "thedev.md").write_text("""---
type: entity
entity_type: person
name: TheDev
importance: 1.0
tags: [owner]
created: 2026-01-01
updated: 2026-04-01
---
Primary user and owner of Hermes.
""")

    # Session
    (vault / "Memory" / "Sessions" / "2026-04-01-1200.md").write_text("""---
type: session
platform: cli
status: closed
open_threads:
  - finish engram
  - push to github
created: 2026-04-01
updated: 2026-04-01
---
Worked on Engram v1 architecture and testing.
""")

    conn = init_db(str(tmp_path / "test.db"))
    full_index(conn, str(vault))
    return conn, vault


def test_get_standing_rules(populated_db):
    from engram.query import get_standing_rules
    conn, _ = populated_db
    rules = get_standing_rules(conn)
    assert len(rules) == 1
    assert "discord" in rules[0]["title"].lower() or "discord-rule" in rules[0]["path"]


def test_get_open_loops(populated_db):
    from engram.query import get_open_loops
    conn, _ = populated_db
    loops = get_open_loops(conn)
    assert len(loops) == 1
    assert "open-loop" in loops[0]["path"]


def test_search_facts_returns_active_only(populated_db):
    from engram.query import search_facts
    conn, _ = populated_db
    results = search_facts(conn, "fact", status="active")
    paths = [r["path"] for r in results]
    assert not any("old-fact" in p for p in paths)


def test_search_facts_by_artifact_type(populated_db):
    from engram.query import search_facts
    conn, _ = populated_db
    results = search_facts(conn, "discord", artifact_type="standing_rule")
    assert len(results) >= 1
    assert all(r["subtype"] == "standing_rule" for r in results)


def test_search_entities(populated_db):
    from engram.query import search_entities
    conn, _ = populated_db
    results = search_entities(conn, name="TheDev")
    assert len(results) >= 1
    assert results[0]["title"] == "TheDev"


def test_get_sessions(populated_db):
    from engram.query import get_sessions
    conn, _ = populated_db
    sessions = get_sessions(conn)
    assert len(sessions) == 1


def test_full_text_search(populated_db):
    from engram.query import full_text_search
    conn, _ = populated_db
    results = full_text_search(conn, "engram")
    assert len(results) >= 1


def test_full_text_search_type_filter(populated_db):
    from engram.query import full_text_search
    conn, _ = populated_db
    results = full_text_search(conn, "TheDev", types=["entity"])
    assert all(r["type"] == "entity" for r in results)


def test_get_top_entities(populated_db):
    from engram.query import get_top_entities
    conn, _ = populated_db
    entities = get_top_entities(conn, limit=5)
    assert len(entities) >= 1
    assert entities[0]["title"] == "TheDev"  # highest importance


def test_get_facts_about(populated_db):
    from engram.query import get_facts_about
    conn, _ = populated_db
    facts = get_facts_about(conn, "TheDev")
    assert len(facts) >= 1


def test_get_recent_facts(populated_db):
    from engram.query import get_recent_facts
    conn, _ = populated_db
    # Use a wide window to catch our test data
    facts = get_recent_facts(conn, days=365)
    assert len(facts) >= 2
    # Superseded facts should not appear
    assert all(f["status"] == "active" for f in facts)
