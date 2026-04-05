"""
Engram MemoryProvider implementation for Hermes.

Obsidian-native persistent memory -- vault is the source of truth,
SQLite FTS5 index for fast queries, lean token-budgeted briefing injection,
mid-session recall via engram_recall tool, auto session capture on end.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _get_engram_modules():
    """Import engram modules. Returns dict or None if not installed."""
    try:
        from engram.schema import init_db
        from engram.indexer import full_index
        from engram.briefing import generate_briefing
        from engram.query import full_text_search, get_standing_rules
        from engram.session import save_session
        return {
            "init_db": init_db,
            "full_index": full_index,
            "generate_briefing": generate_briefing,
            "full_text_search": full_text_search,
            "get_standing_rules": get_standing_rules,
            "save_session": save_session,
        }
    except ImportError as e:
        logger.warning("engram modules not found: %s", e)
        return None


ENGRAM_RECALL_SCHEMA = {
    "name": "engram_recall",
    "description": (
        "Search Jarvis's persistent memory vault for facts, entities, and past sessions.\n\n"
        "USE THIS WHEN:\n"
        "- TheDev references a past decision, project, or preference you don't have context on\n"
        "- You encounter an entity or project name that's unfamiliar\n"
        "- Checking for existing constraints before making a recommendation\n"
        "- TheDev says 'do you remember' or 'last time we'\n"
        "- You want to verify standing rules or preferences\n\n"
        "Returns compact results (title + key fact, max 5). Low token cost."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for (keywords, entity name, topic)",
            },
            "type": {
                "type": "string",
                "enum": ["all", "fact", "entity", "session"],
                "description": "Filter by note type. Default: all",
            },
        },
        "required": ["query"],
    },
}


try:
    from agent.memory_provider import MemoryProvider as _MemoryProvider
except ImportError:
    class _MemoryProvider:  # type: ignore
        """Fallback base when running outside Hermes."""
        pass


class EngramMemoryProvider(_MemoryProvider):
    """Engram memory provider -- Obsidian-native persistent memory for Hermes."""

    def __init__(self):
        self._mods = None
        self._conn = None
        self._vault_root = None
        self._db_path = None
        self._cfg = {}
        self._briefing_cache = ""
        self._platform = "cli"
        self._session_id = ""
        self._agent_context = "primary"
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "engram"

    def is_available(self) -> bool:
        mods = _get_engram_modules()
        if mods is None:
            return False
        cfg = self._load_config()
        vault = Path(cfg.get("vault_path", "~/obsidian-vault")).expanduser()
        return vault.exists()

    def _load_config(self) -> dict:
        cfg_path = Path("~/.engram/config.json").expanduser()
        defaults = {
            "vault_path": "~/obsidian-vault",
            "index_db": "~/.engram/vault_index.db",
            "briefing_budget": 800,
            "briefing_max_facts": 10,
            "briefing_max_entities": 8,
            "ingest_sources": ["Daily", "Projects"],
            "llm_model": "claude-haiku-4-5",
            "session_save_min_turns": 4,
        }
        if cfg_path.exists():
            try:
                defaults.update(json.loads(cfg_path.read_text()))
            except Exception:
                pass
        return defaults

    def initialize(self, session_id: str, **kwargs) -> None:
        self._mods = _get_engram_modules()
        if not self._mods:
            return

        self._cfg = self._load_config()
        self._session_id = session_id
        self._platform = kwargs.get("platform", "cli")
        self._agent_context = kwargs.get("agent_context", "primary")
        self._vault_root = str(Path(self._cfg["vault_path"]).expanduser())
        self._db_path = str(Path(self._cfg["index_db"]).expanduser())

        if self._agent_context not in ("primary",):
            return

        try:
            self._conn = self._mods["init_db"](self._db_path)
            stats = self._mods["full_index"](self._conn, self._vault_root)
            logger.debug("Engram indexed: %s", stats)
            self._briefing_cache = self._mods["generate_briefing"](
                self._conn,
                self._vault_root,
                max_facts=self._cfg.get("briefing_max_facts", 10),
                max_entities=self._cfg.get("briefing_max_entities", 8),
                budget=self._cfg.get("briefing_budget", 800),
                platform=self._platform,
            )
            logger.info("Engram ready: briefing %d chars, platform=%s",
                       len(self._briefing_cache), self._platform)
        except Exception as e:
            logger.warning("Engram init failed (non-fatal): %s", e)

    def system_prompt_block(self) -> str:
        if not self._briefing_cache or self._agent_context != "primary":
            return ""
        return self._briefing_cache

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""  # briefing is injected once via system_prompt_block

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if not self._mods:
            return []
        return [ENGRAM_RECALL_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "engram_recall":
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

        if not self._mods or not self._conn:
            return json.dumps({"error": "Engram not initialized"})

        query = args.get("query", "").strip()
        type_filter = args.get("type", "all")
        if not query:
            return json.dumps({"error": "query is required"})

        try:
            types = None if type_filter == "all" else [type_filter]
            results = self._mods["full_text_search"](
                self._conn, query, types=types, limit=5
            )
            if not results:
                return json.dumps({"results": [], "message": "No matches found."})

            output = []
            for r in results:
                rel_path = r.get("path", "")
                fact_line = ""
                try:
                    import frontmatter
                    full_path = Path(self._vault_root) / rel_path
                    if full_path.exists():
                        post = frontmatter.load(str(full_path))
                        body = post.content.strip()
                        fact_line = body.split("\n")[0][:150] if body else ""
                except Exception:
                    pass
                output.append({
                    "title": r.get("title", rel_path),
                    "type": r.get("subtype") or r.get("type", "note"),
                    "fact": fact_line,
                    "status": r.get("status", ""),
                })
            return json.dumps({"results": output, "count": len(output)})
        except Exception as e:
            logger.warning("engram_recall failed: %s", e)
            return json.dumps({"error": str(e)})

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not self._mods or self._agent_context != "primary":
            return
        min_turns = self._cfg.get("session_save_min_turns", 4)
        user_turns = [m for m in messages if m.get("role") == "user" and m.get("content")]
        if len(user_turns) < min_turns:
            return
        thread = threading.Thread(
            target=self._save_session_async, args=(messages,), daemon=True
        )
        thread.start()

    def _save_session_async(self, messages: List[Dict[str, Any]]) -> None:
        try:
            import re, requests

            turns = []
            for m in messages:
                role = m.get("role", "")
                content = m.get("content", "")
                if role in ("user", "assistant") and content and isinstance(content, str):
                    turns.append(f"{role.upper()}: {content[:500]}")
            if not turns:
                return

            api_key = self._get_api_key()
            if not api_key:
                logger.warning("Engram: no API key for session save")
                return

            conversation = "\n\n".join(turns[-20:])
            provider = self._cfg.get("llm_provider", "openai")
            model = self._cfg.get("llm_model", "gpt-5.4-mini")
            prompt = (
                "Summarize this conversation in 2 sentences max. "
                "Then list any unresolved threads or follow-up items as a bullet list (max 5). "
                "Also list key entities mentioned (people, projects, tools) as a comma-separated list.\n\n"
                "Format your response as JSON:\n"
                '{"summary": "...", "open_threads": ["...", "..."], "entities": ["...", "..."]}\n\n'
                f"CONVERSATION:\n{conversation}"
            )

            if provider == "anthropic":
                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
                msg = client.messages.create(
                    model=model,
                    max_tokens=500,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = msg.content[0].text.strip()
            else:
                # OpenAI-compatible (uses whatever base_url the Hermes provider exposes)
                base_url = getattr(self, "_api_base_url", None) or "https://api.openai.com/v1"
                resp = requests.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 500,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"].strip()

            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)

            self._mods["save_session"](
                vault_root=self._vault_root,
                summary=data.get("summary", "Session completed."),
                open_threads=data.get("open_threads", []),
                entities_referenced=data.get("entities", []),
                platform=self._platform,
            )

            if self._conn:
                with self._lock:
                    self._mods["full_index"](self._conn, self._vault_root)

            self._regenerate_briefing()
            logger.info("Engram: session saved (%d threads, %d entities)",
                       len(data.get("open_threads", [])), len(data.get("entities", [])))
        except Exception as e:
            logger.warning("Engram: session save failed (non-fatal): %s", e)

    def _regenerate_briefing(self) -> None:
        try:
            if not self._conn or not self._mods:
                return
            briefing_md = self._mods["generate_briefing"](
                self._conn, self._vault_root,
                max_facts=self._cfg.get("briefing_max_facts", 10),
                max_entities=self._cfg.get("briefing_max_entities", 8),
                budget=self._cfg.get("briefing_budget", 800),
                platform=self._platform,
            )
            prefill = [
                {"role": "user", "content": briefing_md},
                {"role": "assistant", "content": "Memory loaded."},
            ]
            Path("~/.hermes/engram-briefing.json").expanduser().write_text(
                json.dumps(prefill, indent=2)
            )
        except Exception as e:
            logger.debug("Engram: briefing regen failed: %s", e)

    def _get_api_key(self) -> Optional[str]:
        """Resolve an API key and base URL for session summarization.

        Priority:
        1. ANTHROPIC_API_KEY env var (for Anthropic provider)
        2. Hermes credential pool (reads active provider from config.yaml)
        3. Codex OAuth access token from ~/.codex/auth.json
        4. .env file fallback

        Sets self._api_base_url for the resolved provider.
        """
        provider = self._cfg.get("llm_provider", "hermes")
        self._api_base_url = None

        # Read active provider from Hermes config
        hermes_cfg_path = Path("~/.hermes/config.yaml").expanduser()
        hermes_provider = None
        hermes_base_url = None
        if hermes_cfg_path.exists():
            try:
                import yaml
                cfg = yaml.safe_load(hermes_cfg_path.read_text()) or {}
                hermes_provider = cfg.get("model", {}).get("provider", "")
                hermes_base_url = cfg.get("model", {}).get("base_url", "")
            except Exception:
                pass

        if provider != "anthropic":
            # Try Hermes credential pool — match active provider first
            auth_path = Path("~/.hermes/auth.json").expanduser()
            if auth_path.exists():
                try:
                    data = json.loads(auth_path.read_text())
                    pool = data.get("credential_pool", {})
                    # Try the active Hermes provider first
                    if hermes_provider and hermes_provider in pool:
                        for creds in pool[hermes_provider]:
                            token = creds.get("access_token", "")
                            if token and len(token) > 10:
                                self._api_base_url = creds.get("base_url") or hermes_base_url
                                return token
                    # Fallback: scan all providers
                    for prov_name, creds_list in pool.items():
                        if prov_name == "anthropic":
                            continue
                        for creds in creds_list:
                            token = creds.get("access_token", "")
                            if token and len(token) > 10:
                                self._api_base_url = creds.get("base_url") or hermes_base_url
                                return token
                except Exception:
                    pass

            # Codex OAuth token
            codex_path = Path("~/.codex/auth.json").expanduser()
            if codex_path.exists():
                try:
                    data = json.loads(codex_path.read_text())
                    token = data.get("accessToken", "")
                    if token and len(token) > 10:
                        return token
                except Exception:
                    pass

        # Anthropic env var
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if key and key != "***":
            return key

        # .env file
        env_path = Path("~/.hermes/.env").expanduser()
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if k in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
                         "ANTHROPIC_API_KEY_2", "ANTHROPIC_API_KEY_3"):
                    if v and v != "***":
                        return v
        return None

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        if not self._mods or not self._conn or self._agent_context != "primary":
            return ""
        try:
            rules = self._mods["get_standing_rules"](self._conn)
            if rules:
                return "Engram standing rules:\n" + "\n".join(
                    f"- {r['title']}" for r in rules[:3]
                )
        except Exception:
            pass
        return ""

    def shutdown(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "vault_path",
                "description": "Path to your Obsidian vault",
                "required": False,
                "default": "~/obsidian-vault",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        cfg_dir = Path("~/.engram").expanduser()
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / "config.json"
        existing = {}
        if cfg_path.exists():
            try:
                existing = json.loads(cfg_path.read_text())
            except Exception:
                pass
        existing.update(values)
        cfg_path.write_text(json.dumps(existing, indent=2))
