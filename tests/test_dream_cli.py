import json
from pathlib import Path

from click.testing import CliRunner

from engram.cli import cli
from engram.schema import init_db
from engram.indexer import full_index


def _write_config(config_path: Path, vault_path: Path, db_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "vault_path": str(vault_path),
                "index_db": str(db_path),
                "log_level": "warning",
                "log_path": str(config_path.parent / "engram.log"),
            }
        ),
        encoding="utf-8",
    )


def _write_note(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_vault(vault: Path) -> None:
    _write_note(
        vault / "Daily" / "2026-04-08.md",
        "# 2026-04-08\n\n- checked dream mode\n",
    )
    _write_note(
        vault / "Memory" / "Facts" / "rule.md",
        """---
type: fact
artifact_type: standing_rule
title: Keep replies concise
status: active
importance: 0.9
updated: 2026-04-08
---
Body
""",
    )
    _write_note(
        vault / "Memory" / "Entities" / "jarvis.md",
        """---
type: entity
entity_type: agent
title: Jarvis
importance: 1.0
updated: 2026-04-08
---
Body
""",
    )
    _write_note(
        vault / "Memory" / "Sessions" / "session-1.md",
        """---
type: session
title: Dream command recovery
created: 2026-04-08
---
Body
""",
    )


def test_dream_dry_run_outputs_report(tmp_path: Path):
    vault = tmp_path / "vault"
    db_path = tmp_path / "engram.db"
    config_path = tmp_path / "config.json"
    _seed_vault(vault)
    _write_config(config_path, vault, db_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["--config", str(config_path), "dream", "--dry-run"])

    assert result.exit_code == 0
    assert "Engram dream (dry-run)" in result.output
    assert "Index pass:   skipped (dry-run)" in result.output
    assert "Recent Daily notes:" in result.output


def test_dream_live_updates_index_and_json(tmp_path: Path):
    vault = tmp_path / "vault"
    db_path = tmp_path / "engram.db"
    config_path = tmp_path / "config.json"
    _seed_vault(vault)
    _write_config(config_path, vault, db_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["--config", str(config_path), "dream", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["dry_run"] is False
    assert payload["index_after"] >= 4
    assert payload["standing_rules"][0]["title"] == "Keep replies concise"
    assert payload["top_entities"][0]["title"] == "Jarvis"
