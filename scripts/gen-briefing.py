#!/usr/bin/env python3
"""
Generate Engram briefing and write it as a Hermes prefill JSON file.

Usage: python3 gen-briefing.py [--config ~/.engram/config.json] [--out ~/.hermes/engram-briefing.json] [--platform cli|discord] [--reindex]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engram.schema import init_db
from engram.briefing import generate_briefing
from engram.indexer import full_index


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="~/.engram/config.json")
    parser.add_argument("--out", default="~/.hermes/engram-briefing.json")
    parser.add_argument("--platform", default=None, help="cli or discord")
    parser.add_argument("--budget", type=int, default=800, help="Max chars in briefing")
    parser.add_argument("--reindex", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    cfg = {
        "vault_path": "~/obsidian-vault",
        "index_db": "~/.engram/vault_index.db",
        "briefing_max_facts": 10,
        "briefing_max_entities": 8,
    }
    if config_path.exists():
        with open(config_path) as f:
            cfg.update(json.load(f))

    vault = str(Path(cfg["vault_path"]).expanduser())
    db_path = str(Path(cfg["index_db"]).expanduser())

    conn = init_db(db_path)
    if args.reindex:
        full_index(conn, vault)

    briefing_md = generate_briefing(
        conn,
        vault,
        max_facts=cfg.get("briefing_max_facts", 10),
        max_entities=cfg.get("briefing_max_entities", 8),
        budget=args.budget,
        platform=args.platform,
    )

    # Hermes prefill format
    prefill = [
        {"role": "user", "content": briefing_md},
        {"role": "assistant", "content": "Memory loaded."}
    ]

    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(prefill, f, indent=2)

    print(f"Briefing: {len(briefing_md)} chars / {args.budget} budget ({100*len(briefing_md)//args.budget}% used)")
    print(briefing_md)


if __name__ == "__main__":
    main()
