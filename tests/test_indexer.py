"""Tests for engram.indexer -- parsing, indexing, and change detection."""

import os
import tempfile
import time
import pytest
from pathlib import Path

from engram.schema import init_db
from engram.indexer import full_index, _parse_note, _upsert_note


@pytest.fixture
def vault(tmp_path):
    """Create a minimal vault structure."""
    (tmp_path / "Memory" / "Facts").mkdir(parents=True)
    (tmp_path / "Memory" / "Entities").mkdir(parents=True)
    (tmp_path / "Memory" / "Sessions").mkdir(parents=True)
    (tmp_path / "Daily").mkdir()
    return tmp_path


@pytest.fixture
def db(tmp_path):
    return init_db(str(tmp_path / "test.db"))


def write_note(vault, rel_path, content):
    path = vault / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_parse_entity_note(vault):
    path = write_note(vault, "Memory/Entities/thedev.md", """---
type: entity
entity_type: person
name: TheDev
importance: 1.0
tags: [owner]
created: 2026-01-01
updated: 2026-01-01
---

Primary user and owner.
""")
    note = _parse_note(str(path), str(vault))
    assert note is not None
    assert note["type"] == "entity"
    assert note["subtype"] == "person"
    assert note["title"] == "TheDev"
    assert note["importance"] == 1.0
    assert "owner" in note["tags"]


def test_parse_fact_note(vault):
    path = write_note(vault, "Memory/Facts/test-rule.md", """---
type: fact
artifact_type: standing_rule
status: active
importance: 1.0
confidence: 1.0
tags: [discord]
created: 2026-01-01
updated: 2026-01-01
---

Always use inline components.
""")
    note = _parse_note(str(path), str(vault))
    assert note is not None
    assert note["type"] == "fact"
    assert note["subtype"] == "standing_rule"
    assert note["importance"] == 1.0
    assert note["status"] == "active"


def test_parse_infers_type_from_path(vault):
    path = write_note(vault, "Memory/Entities/project.md", "---\n---\n\nNo type field.\n")
    note = _parse_note(str(path), str(vault))
    assert note["type"] == "entity"


def test_parse_extracts_wikilinks(vault):
    path = write_note(vault, "Memory/Facts/linked.md", """---
type: fact
artifact_type: durable_fact
status: active
importance: 0.5
confidence: 0.8
about:
  - '[[TheDev]]'
created: 2026-01-01
updated: 2026-01-01
---

A fact about [[Engram]] and [[TheDev]].
""")
    note = _parse_note(str(path), str(vault))
    link_targets = [l[0] for l in note["links"]]
    assert "TheDev" in link_targets
    assert "Engram" in link_targets


def test_full_index_counts(vault, db):
    write_note(vault, "Memory/Facts/f1.md", "---\ntype: fact\nartifact_type: durable_fact\nstatus: active\nimportance: 0.5\nconfidence: 0.8\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\nFact one.\n")
    write_note(vault, "Memory/Entities/e1.md", "---\ntype: entity\nentity_type: person\nname: Alice\nimportance: 0.7\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\nSome person.\n")

    stats = full_index(db, str(vault))
    assert stats["errors"] == 0
    count = db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    assert count == 2


def test_incremental_skip_unchanged(vault, db):
    write_note(vault, "Memory/Facts/f1.md", "---\ntype: fact\nartifact_type: durable_fact\nstatus: active\nimportance: 0.5\nconfidence: 0.8\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\nFact one.\n")

    stats1 = full_index(db, str(vault))
    stats2 = full_index(db, str(vault))
    # Second run should skip the unchanged file
    assert stats2["skipped"] == 1
    assert stats2["updated"] == 0


def test_index_detects_changes(vault, db):
    path = write_note(vault, "Memory/Facts/f1.md", "---\ntype: fact\nartifact_type: durable_fact\nstatus: active\nimportance: 0.5\nconfidence: 0.8\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\nOriginal content.\n")
    full_index(db, str(vault))

    # Modify the file
    time.sleep(0.05)
    path.write_text("---\ntype: fact\nartifact_type: durable_fact\nstatus: active\nimportance: 0.8\nconfidence: 0.9\ncreated: 2026-01-01\nupdated: 2026-01-02\n---\nUpdated content.\n")

    stats = full_index(db, str(vault))
    assert stats["updated"] == 1

    # Importance should be updated
    row = db.execute("SELECT importance FROM docs WHERE path LIKE '%f1.md'").fetchone()
    assert abs(row[0] - 0.8) < 0.01


def test_index_removes_deleted(vault, db):
    path = write_note(vault, "Memory/Facts/f1.md", "---\ntype: fact\nartifact_type: durable_fact\nstatus: active\nimportance: 0.5\nconfidence: 0.8\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\nWill be deleted.\n")
    full_index(db, str(vault))

    path.unlink()
    full_index(db, str(vault))

    count = db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    assert count == 0
