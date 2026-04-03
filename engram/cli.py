"""
Engram CLI — engram <command>

Commands:
  index    -- scan vault and build/update SQLite index
  brief    -- generate and print session briefing
  search   -- full-text search the vault
  ingest   -- run LLM extraction pipeline
  session  -- save a session snapshot
  version  -- print version and index stats
"""

import json
import logging
import os
import sys
from pathlib import Path

import click

from . import __version__
from .schema import init_db, get_meta
from .indexer import full_index, start_watcher
from .query import full_text_search, get_standing_rules
from .briefing import generate_briefing

DEFAULT_CONFIG = Path("~/.engram/config.json").expanduser()


def load_config(config_path: str = None) -> dict:
    path = Path(config_path or DEFAULT_CONFIG).expanduser()
    defaults = {
        "vault_path": "~/obsidian-vault",
        "index_db": "~/.engram/vault_index.db",
        "ingest_interval_minutes": 60,
        "llm_model": "claude-haiku-4-5",
        "ingest_sources": ["Daily", "Projects"],
        "min_confidence": 0.6,
        "briefing_max_facts": 10,
        "briefing_max_entities": 8,
        "briefing_budget": 800,
        "log_level": "info",
        "log_path": "~/.engram/engram.log",
    }
    if path.exists():
        with open(path) as f:
            loaded = json.load(f)
        defaults.update(loaded)
    elif config_path:
        # Only warn if they explicitly passed a config path that doesn't exist
        click.echo(f"Warning: config not found at {path}, using defaults", err=True)
    return defaults


def _check_vault(cfg: dict) -> Path:
    """Validate vault exists, give helpful error if not."""
    vault = Path(cfg["vault_path"]).expanduser()
    if not vault.exists():
        click.echo(f"Error: vault not found at {vault}", err=True)
        click.echo("Run 'engram init' to set up your vault.", err=True)
        raise SystemExit(1)
    return vault


def _check_index(cfg: dict) -> str:
    """Return DB path, warn if index has never been built."""
    db_path = str(Path(cfg["index_db"]).expanduser())
    if not Path(db_path).exists():
        click.echo("Index not built yet. Run 'engram index' first.", err=True)
    return db_path


def setup_logging(level: str, log_path: str = None):
    numeric = getattr(logging, level.upper(), logging.INFO)
    handlers = [logging.StreamHandler(sys.stderr)]
    if log_path:
        lp = Path(log_path).expanduser()
        lp.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(str(lp)))
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=handlers,
    )


@click.group()
@click.option("--config", default=None, help="Path to config.json")
@click.pass_context
def cli(ctx, config):
    ctx.ensure_object(dict)
    cfg = load_config(config)
    setup_logging(cfg.get("log_level", "info"), cfg.get("log_path"))
    ctx.obj["cfg"] = cfg


@cli.command()
@click.option("--vault", default=None, help="Path to your Obsidian vault")
@click.option("--api-key", default=None, help="Anthropic API key (for ingest)")
@click.option("--yes", is_flag=True, help="Skip confirmation prompts")
def init(vault, api_key, yes):
    """Set up Engram for first use. Creates config, vault structure, and AGENTS.md."""
    import shutil

    click.echo("Engram init\n")

    # --- Vault path ---
    if not vault:
        default_vault = str(Path("~/obsidian-vault").expanduser())
        vault = click.prompt("Obsidian vault path", default=default_vault)
    vault_path = Path(vault).expanduser()

    if not vault_path.exists():
        if yes or click.confirm(f"Vault path {vault_path} doesn't exist. Create it?", default=True):
            vault_path.mkdir(parents=True, exist_ok=True)
            click.echo(f"  Created: {vault_path}")
        else:
            click.echo("Aborted.")
            return

    # --- Create vault structure ---
    for folder in ["Memory/Facts", "Memory/Entities", "Memory/Sessions", "Daily", "Projects"]:
        (vault_path / folder).mkdir(parents=True, exist_ok=True)
    click.echo(f"  Vault structure ready: {vault_path}")

    # --- Copy AGENTS.md template ---
    agents_dest = vault_path / "AGENTS.md"
    if not agents_dest.exists():
        template_src = Path(__file__).parent.parent / "templates" / "AGENTS.md"
        if template_src.exists():
            shutil.copy(str(template_src), str(agents_dest))
            click.echo(f"  Installed: {agents_dest}")
        else:
            click.echo("  Warning: AGENTS.md template not found -- skipping")
    else:
        click.echo(f"  Exists (skipped): {agents_dest}")

    # --- API key ---
    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key and not yes:
            api_key = click.prompt(
                "Anthropic API key (for ingest pipeline, leave blank to skip)",
                default="",
                show_default=False,
            )

    # --- Write config ---
    config_dir = Path("~/.engram").expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"

    config_data = {
        "vault_path": str(vault_path),
        "index_db": str(config_dir / "vault_index.db"),
        "ingest_interval_minutes": 60,
        "llm_model": "claude-haiku-4-5",
        "llm_provider": "anthropic",
        "ingest_sources": ["Daily", "Projects"],
        "min_confidence": 0.6,
        "briefing_max_facts": 10,
        "briefing_max_entities": 8,
        "briefing_budget": 800,
        "log_level": "info",
        "log_path": str(config_dir / "engram.log"),
    }

    if config_path.exists() and not yes:
        if not click.confirm(f"Config exists at {config_path}. Overwrite?", default=False):
            click.echo("  Keeping existing config.")
        else:
            with open(config_path, "w") as f:
                import json
                json.dump(config_data, f, indent=2)
            click.echo(f"  Config written: {config_path}")
    else:
        with open(config_path, "w") as f:
            import json
            json.dump(config_data, f, indent=2)
        click.echo(f"  Config written: {config_path}")

    # --- Store API key hint ---
    if api_key:
        env_path = config_dir / ".env"
        env_path.write_text(f"ANTHROPIC_API_KEY={api_key}\n")
        env_path.chmod(0o600)
        click.echo(f"  API key saved: {env_path}")

    # --- Build initial index ---
    click.echo("\nBuilding initial index...")
    from .schema import init_db
    from .indexer import full_index
    conn = init_db(str(config_dir / "vault_index.db"))
    stats = full_index(conn, str(vault_path))
    click.echo(f"  Indexed: {stats['new'] + stats['updated']} notes, {stats['errors']} errors")

    click.echo("\nDone. Next steps:")
    click.echo("  engram brief          -- see your session briefing")
    click.echo("  engram ingest         -- extract facts from Daily/Projects notes")
    click.echo("  engram search <query> -- search your vault")
    click.echo("")
    click.echo("To wire into Hermes, add to ~/.hermes/config.yaml:")
    click.echo('  prefill_messages_file: "engram-briefing.json"')
    click.echo("Then run: python3 scripts/gen-briefing.py")


@cli.command()
@click.option("--watch", is_flag=True, help="Keep running and watch for changes")
@click.pass_context
def index(ctx, watch):
    """Scan vault and build/update the SQLite index."""
    cfg = ctx.obj["cfg"]
    vault = str(_check_vault(cfg))
    db_path = str(Path(cfg["index_db"]).expanduser())

    conn = init_db(db_path)
    click.echo(f"Indexing vault: {vault}")
    stats = full_index(conn, vault)
    click.echo(
        f"Done. new={stats['new']} updated={stats['updated']} "
        f"skipped={stats['skipped']} errors={stats['errors']}"
    )

    if watch:
        click.echo("Watching for changes (Ctrl+C to stop)...")
        observer = start_watcher(conn, vault)
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()


@cli.command()
@click.pass_context
def brief(ctx):
    """Generate and print the session briefing."""
    cfg = ctx.obj["cfg"]
    vault = str(Path(cfg["vault_path"]).expanduser())
    db_path = str(Path(cfg["index_db"]).expanduser())

    conn = init_db(db_path)
    briefing = generate_briefing(
        conn,
        vault,
        max_facts=cfg.get("briefing_max_facts", 15),
        max_entities=cfg.get("briefing_max_entities", 10),
    )
    click.echo(briefing)


@cli.command()
@click.argument("query")
@click.option("--type", "note_type", default=None, help="Filter by type: fact, entity, session")
@click.option("--limit", default=10, help="Max results")
@click.pass_context
def search(ctx, query, note_type, limit):
    """Full-text search the vault index."""
    cfg = ctx.obj["cfg"]
    db_path = str(Path(cfg["index_db"]).expanduser())

    conn = init_db(db_path)
    types = [note_type] if note_type else None
    results = full_text_search(conn, query, types=types, limit=limit)

    if not results:
        click.echo("No results.")
        return

    for r in results:
        subtype = f" [{r.get('subtype', '')}]" if r.get("subtype") else ""
        status = f" ({r.get('status', '')})" if r.get("status") else ""
        click.echo(f"\n{r['title']}{subtype}{status}")
        click.echo(f"  {r['path']}")
        if r.get("excerpt"):
            click.echo(f"  ...{r['excerpt']}...")


@cli.command()
@click.option("--sources", default=None, help="Comma-separated source dirs (relative to vault or absolute)")
@click.pass_context
def ingest(ctx, sources):
    """Run LLM extraction pipeline and write facts/entities to vault."""
    from .ingest import run_ingest

    cfg = ctx.obj["cfg"]
    vault = str(_check_vault(cfg))
    db_path = str(Path(cfg["index_db"]).expanduser())

    source_dirs = sources.split(",") if sources else cfg.get("ingest_sources", ["Daily"])
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    click.echo(f"Running ingest from: {source_dirs}")
    stats = run_ingest(
        vault_root=vault,
        source_dirs=source_dirs,
        model=cfg.get("llm_model", "claude-haiku-4-5"),
        api_key=api_key,
    )
    click.echo(
        f"Ingest complete. files={stats['files_processed']} "
        f"entities={stats['entities_created']} facts={stats['facts_created']} "
        f"errors={stats['errors']}"
    )

    # Re-index after ingest
    conn = init_db(db_path)
    full_index(conn, vault)
    click.echo("Index updated.")


@cli.command()
@click.argument("summary")
@click.option("--threads", default="", help="Comma-separated open threads")
@click.option("--entities", default="", help="Comma-separated entity names")
@click.option("--platform", default="cli")
@click.pass_context
def session(ctx, summary, threads, entities, platform):
    """Save a session snapshot to the vault."""
    from .session import save_session

    cfg = ctx.obj["cfg"]
    vault = str(Path(cfg["vault_path"]).expanduser())

    open_threads = [t.strip() for t in threads.split(",") if t.strip()]
    entity_list = [e.strip() for e in entities.split(",") if e.strip()]

    path = save_session(
        vault_root=vault,
        summary=summary,
        open_threads=open_threads,
        entities_referenced=entity_list,
        platform=platform,
    )
    click.echo(f"Session saved: {path}")


@cli.command()
@click.pass_context
def version(ctx):
    """Print version and index stats."""
    cfg = ctx.obj["cfg"]
    db_path = str(Path(cfg["index_db"]).expanduser())

    click.echo(f"Engram v{__version__}")

    conn = init_db(db_path)
    schema_ver = get_meta(conn, "schema_version", "unknown")
    last_indexed = get_meta(conn, "last_indexed", "never")

    total = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    by_type = conn.execute(
        "SELECT type, COUNT(*) as n FROM docs GROUP BY type"
    ).fetchall()

    click.echo(f"Schema:       {schema_ver}")
    click.echo(f"Last indexed: {last_indexed}")
    click.echo(f"Total notes:  {total}")
    for row in by_type:
        click.echo(f"  {row[0]}: {row[1]}")
    click.echo(f"Vault:        {cfg['vault_path']}")
    click.echo(f"Index DB:     {cfg['index_db']}")
