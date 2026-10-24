"""
Ingest pipeline — reads source files, runs LLM extraction,
writes Entity and Fact notes back to the vault.
"""

import json
import logging
import os
import re
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("engram.ingest")

EXTRACTION_PROMPT = """\
You are extracting durable memory from a document for an AI agent named Jarvis.

RULES:
- Only extract facts worth keeping 6+ months from now
- Prefer 3 high-quality facts over 15 mediocre ones
- Every fact must have an explicit artifact_type
- Assign confidence >= 0.6 or discard
- standing_rules always get importance 1.0
- Do NOT extract temporary status, current task details, or obvious things

ARTIFACT TYPES:
  durable_fact   -- stable truth, long shelf life
  preference     -- how TheDev likes things done
  standing_rule  -- always apply (e.g. use inline Discord components)
  decision       -- something decided with context
  lesson         -- learned from failure or success
  open_loop      -- unresolved thread to revisit
  constraint     -- hard limit or boundary

OUTPUT: Valid JSON only. No markdown fences, no explanation.

{
  "entities": [
    {
      "name": "string",
      "entity_type": "person|project|tool|concept|place|org",
      "description": "string",
      "importance": 0.0-1.0,
      "tags": ["tag1", "tag2"]
    }
  ],
  "facts": [
    {
      "title": "short descriptive title",
      "content": "the fact itself, one clear statement",
      "artifact_type": "one of the types above",
      "confidence": 0.0-1.0,
      "importance": 0.0-1.0,
      "about": ["Entity Name"],
      "tags": ["tag1", "tag2"]
    }
  ]
}

DOCUMENT:
{document}
"""


def _chunk_file(path: str, max_chars: int = 6000) -> list:
    """Split a markdown file into chunks by H2/H3 headings."""
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read()

    sections = re.split(r"(?m)^#{2,3}\s+", content)
    chunks = []
    current = ""

    for section in sections:
        if len(current) + len(section) < max_chars:
            current += "\n\n" + section
        else:
            if current.strip():
                chunks.append(current.strip())
            current = section

    if current.strip():
        chunks.append(current.strip())

    return chunks or [content[:max_chars]]


def _extract_json_text(text: str) -> str:
    """Return the complete JSON object inside model output (NUBIS-001).

    Reasoning-only completions often wrap a formatted, multi-line JSON answer in
    prose or a ```json fence. Try, in order: the whole text, the last fenced
    block, then the widest balanced {...} span that parses. Non-JSON text is
    returned unchanged so the caller's json.loads still fails safely.
    """
    candidates = [text.strip()]
    candidates += [m.strip() for m in reversed(re.findall(r"```(?:json)?\s*(.*?)```", text, re.S))]
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for start in starts:
        depth, in_str, esc = 0, False, False
        for j in range(start, len(text)):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:j + 1])
                    break
    best = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and (best is None or len(cand) > len(best)):
            best = cand
    return best if best is not None else text.strip()


def _call_llm(chunk: str, model: str, api_key: str, existing_titles: list = None,
              base_url: str = None) -> Optional[dict]:
    """Call LLM API for extraction. Supports Anthropic and OpenAI-compatible endpoints.
    Returns parsed JSON or None."""
    try:
        # Inject existing titles so LLM avoids extracting duplicates
        existing_block = ""
        if existing_titles:
            titles_list = "\n".join(f"- {t}" for t in existing_titles[:50])
            existing_block = f"\nALREADY STORED (do NOT re-extract these or close variants):\n{titles_list}\n"
        prompt = EXTRACTION_PROMPT.replace("{document}", existing_block + "\n" + chunk)

        if base_url:
            # OpenAI-compatible endpoint
            import requests
            url = base_url.rstrip("/") + "/chat/completions"
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            raw = msg.get("content", "") or ""
            if not raw.strip():
                # Reasoning models may put output in reasoning_content
                reasoning = msg.get("reasoning_content", "") or ""
                if reasoning.strip():
                    raw = _extract_json_text(reasoning)
        else:
            # Anthropic native
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()

        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse error: {e}")
        return None
    except Exception as e:
        logger.warning(f"LLM call failed: {e}")
        return None


def _write_entity(vault_root: Path, entity: dict) -> Optional[str]:
    """Write an entity note to Memory/Entities/. Returns path or None."""
    name = entity.get("name", "").strip()
    if not name:
        return None

    entities_dir = vault_root / "Memory" / "Entities"
    entities_dir.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[\s_]+", "-", slug)[:50]
    filepath = entities_dir / f"{slug}.md"

    if filepath.exists():
        return None  # Already exists, don't overwrite

    tags = entity.get("tags", [])
    tags_yaml = "[" + ", ".join(tags) + "]" if tags else "[]"
    today = date.today().isoformat()

    content = f"""---
type: entity
entity_type: {entity.get('entity_type', 'concept')}
name: {name}
importance: {entity.get('importance', 0.5):.1f}
tags: {tags_yaml}
created: {today}
updated: {today}
---

{entity.get('description', '')}
"""
    filepath.write_text(content, encoding="utf-8")
    logger.info(f"Entity created: {filepath.name}")
    return str(filepath)


def _existing_fact_contents(vault_root: Path) -> list:
    """Return list of (title_keywords, body_keywords) for existing facts."""
    facts_dir = vault_root / "Memory" / "Facts"
    if not facts_dir.exists():
        return []
    existing = []
    for fpath in facts_dir.glob("*.md"):
        try:
            post = frontmatter.load(str(fpath))
            title = post.metadata.get("title") or fpath.stem
            body = post.content or ""
            # Use first line of body as the fact statement
            body_first = body.strip().split("\n")[0].strip() if body.strip() else body
            existing.append((_keywords(title), _keywords(body_first)))
        except Exception:
            pass
    return existing


_STOPWORDS = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
              "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
              "this", "that", "it", "its", "not", "no", "never", "always",
              "must", "should", "will", "can", "do", "does", "has", "have",
              "from", "by", "as", "all", "any", "after", "before", "during"}


def _keywords(text: str) -> set:
    """Extract meaningful keywords -- strip stopwords and short tokens."""
    tokens = set(re.findall(r"\w+", text.lower()))
    return {t for t in tokens if len(t) > 3 and t not in _STOPWORDS}


def _is_duplicate_fact(title: str, content: str, existing: list, threshold: float = 0.4) -> bool:
    """
    Check if a fact is a duplicate using keyword overlap (stopwords removed).
    Checks both title and content. Returns True if too similar to any existing fact.
    """
    new_title_kw = _keywords(title)
    new_body_kw = _keywords(content)

    for ex_title_kw, ex_body_kw in existing:
        # Title keyword overlap
        if new_title_kw and ex_title_kw:
            union = new_title_kw | ex_title_kw
            if union:
                t_overlap = len(new_title_kw & ex_title_kw) / len(union)
                if t_overlap >= threshold:
                    return True
        # Content keyword overlap (only meaningful chunks)
        if len(new_body_kw) > 5 and len(ex_body_kw) > 5:
            union = new_body_kw | ex_body_kw
            if union:
                b_overlap = len(new_body_kw & ex_body_kw) / len(union)
                if b_overlap >= threshold:
                    return True
    return False


def _write_fact(vault_root: Path, fact: dict, existing: list = None) -> Optional[str]:
    """Write a fact note to Memory/Facts/. Returns path or None."""
    title = fact.get("title", "").strip()
    content_body = fact.get("content", "").strip()
    artifact_type = fact.get("artifact_type", "durable_fact")
    confidence = float(fact.get("confidence", 0.7))
    importance = float(fact.get("importance", 0.5))

    if not title or confidence < 0.6:
        return None

    # standing_rules always get max importance
    if artifact_type == "standing_rule":
        importance = 1.0

    facts_dir = vault_root / "Memory" / "Facts"
    facts_dir.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_]+", "-", slug)[:60]
    today = date.today().isoformat()
    filepath = facts_dir / f"{today}-{slug}.md"

    if filepath.exists():
        return None  # Exact filename dedup

    # Semantic dedup -- skip near-duplicate titles or content
    if existing and _is_duplicate_fact(title, content_body, existing):
        logger.debug(f"Skipping duplicate fact: {title}")
        return None

    about = fact.get("about", [])
    about_yaml = "\n".join(f"  - '[[{e}]]'" for e in about) if about else "  []"
    tags = fact.get("tags", [])
    tags_yaml = "[" + ", ".join(tags) + "]" if tags else "[]"

    note = f"""---
type: fact
artifact_type: {artifact_type}
status: active
importance: {importance:.1f}
confidence: {confidence:.2f}
about:
{about_yaml}
tags: {tags_yaml}
source: ingest
created: {today}
updated: {today}
---

{content_body}
"""
    filepath.write_text(note, encoding="utf-8")
    logger.info(f"Fact created: {filepath.name}")
    return str(filepath)


def run_ingest(
    vault_root: str,
    source_dirs: list,
    model: str = "claude-haiku-4-5",
    api_key: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    max_workers: int = 5,
) -> dict:
    """
    Main ingest entry point.
    Reads source_dirs, extracts entities/facts, writes to vault.
    """
    vault_root = Path(vault_root).expanduser()
    stats = {
        "files_processed": 0,
        "chunks_processed": 0,
        "entities_created": 0,
        "facts_created": 0,
        "errors": 0,
    }

    # Fill provider/model/endpoint from the Engram config. Explicit arguments win (NUBIS-003).
    cfg = {}
    cfg_path = Path("~/.engram/config.json").expanduser()
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
        except Exception:
            cfg = {}
    if not provider:
        provider = cfg.get("llm_provider") or "anthropic"
    if model == "claude-haiku-4-5" and cfg.get("llm_model"):
        model = cfg["llm_model"]
    if not base_url and provider != "anthropic":
        base_url = cfg.get("llm_base_url") or None
    explicit_endpoint = bool(base_url)
    if not api_key and provider != "anthropic":
        api_key = os.environ.get(cfg.get("llm_api_key_env") or "OPENAI_API_KEY") or None

    if not api_key and provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and provider == "anthropic":
        # Try loading from ~/.hermes/.env directly
        # Check multiple key names in priority order
        env_path = Path("~/.hermes/.env").expanduser()
        if env_path.exists():
            candidates = {}
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
                         "ANTHROPIC_API_KEY_2", "ANTHROPIC_API_KEY_3"):
                    if v and v != "***":
                        candidates[k] = v
            # Priority: main key > token > key_2 > key_3
            for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
                         "ANTHROPIC_API_KEY_2", "ANTHROPIC_API_KEY_3"):
                if name in candidates:
                    api_key = candidates[name]
                    break

    # Resolve base_url for OpenAI-compatible providers (also provides api_key from credential pool)
    # A configured/caller endpoint is never replaced by a credential-pool endpoint.
    if provider != "anthropic" and not explicit_endpoint and not (api_key and base_url):
        # Only check credential pool if credentials weren't fully provided
        auth_path = Path("~/.hermes/auth.json").expanduser()
        if auth_path.exists():
            try:
                auth_data = json.loads(auth_path.read_text())
                pool = auth_data.get("credential_pool", {})
                hermes_cfg_path = Path("~/.hermes/config.yaml").expanduser()
                hermes_provider = None
                hermes_base_url = None
                if hermes_cfg_path.exists():
                    import yaml
                    hcfg = yaml.safe_load(hermes_cfg_path.read_text()) or {}
                    hermes_provider = hcfg.get("model", {}).get("provider", "")
                    hermes_base_url = hcfg.get("model", {}).get("base_url", "")
                if hermes_provider and hermes_provider in pool:
                    for creds in pool[hermes_provider]:
                        token = creds.get("access_token", "")
                        if token and len(token) > 10:
                            base_url = creds.get("base_url") or hermes_base_url
                            api_key = token
                            break
                if not base_url:
                    for prov_name, creds_list in pool.items():
                        if prov_name == "anthropic":
                            continue
                        for creds in creds_list:
                            token = creds.get("access_token", "")
                            if token and len(token) > 10:
                                base_url = creds.get("base_url") or hermes_base_url
                                api_key = token
                                break
                        if base_url:
                            break
            except Exception as e:
                logger.warning(f"Failed to read Hermes auth.json: {e}")

    if not api_key:
        raise ValueError("No API key -- set ANTHROPIC_API_KEY or pass api_key")

    # Collect all source files
    source_files = []
    for src_dir in source_dirs:
        src_path = vault_root / src_dir
        if not src_path.exists():
            # Try as absolute path
            src_path = Path(src_dir).expanduser()
        if not src_path.exists():
            logger.warning(f"Source dir not found: {src_dir}")
            continue
        for path in src_path.rglob("*.md"):
            if not path.name.startswith("."):
                source_files.append(str(path))

    # Load ingest state -- track which files have been processed
    state_path = Path("~/.engram/ingest_state.json").expanduser()
    ingest_state = {}
    if state_path.exists():
        try:
            ingest_state = json.loads(state_path.read_text())
        except Exception:
            pass

    # Filter to only new or changed files
    filtered_files = []
    for fpath in source_files:
        mtime = round(os.path.getmtime(fpath), 3)
        prev_mtime = ingest_state.get(fpath)
        if prev_mtime is None or abs(prev_mtime - mtime) > 0.01:
            filtered_files.append((fpath, mtime))
        else:
            stats["skipped"] = stats.get("skipped", 0) + 1

    logger.info(f"Ingesting {len(source_files)} files from {len(source_dirs)} sources")

    if not filtered_files:
        logger.info("No new or changed files to ingest.")
        return stats

    logger.info(f"{len(filtered_files)} new/changed files to process ({stats.get('skipped', 0)} unchanged skipped)")

    # Chunk only new/changed files
    chunks = []
    for fpath, mtime in filtered_files:
        stats["files_processed"] += 1
        for chunk in _chunk_file(fpath):
            if len(chunk.strip()) > 100:  # Skip tiny chunks
                chunks.append(chunk)

    logger.info(f"Processing {len(chunks)} chunks with {max_workers} workers")

    # Pre-load existing facts for dedup
    existing_facts = _existing_fact_contents(vault_root)

    # Collect existing fact content for LLM dedup context (first line of body)
    existing_titles_list = []
    facts_dir = vault_root / "Memory" / "Facts"
    if facts_dir.exists():
        for fpath in facts_dir.glob("*.md"):
            try:
                post = frontmatter.load(str(fpath))
                # Prefer body first line (the actual fact statement) over slug title
                body_first = post.content.strip().split("\n")[0].strip() if post.content.strip() else ""
                t = body_first or post.metadata.get("title") or fpath.stem
                if t:
                    existing_titles_list.append(t)
            except Exception:
                pass

    # Parallel LLM extraction
    def process_chunk(chunk):
        return _call_llm(chunk, model, api_key, existing_titles=existing_titles_list, base_url=base_url)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_chunk, c): c for c in chunks}
        for future in as_completed(futures):
            stats["chunks_processed"] += 1
            result = future.result()
            if result is None:
                stats["errors"] += 1
                continue

            for entity in result.get("entities", []):
                path = _write_entity(vault_root, entity)
                if path:
                    stats["entities_created"] += 1

            for fact in result.get("facts", []):
                path = _write_fact(vault_root, fact, existing_facts)
                if path:
                    stats["facts_created"] += 1
                    # Add to dedup set immediately so parallel chunks don't re-create
                    title_tok = set(re.findall(r"\w+", fact.get("title", "").lower()))
                    body_tok = set(re.findall(r"\w+", fact.get("content", "").lower()))
                    existing_facts.append((title_tok, body_tok))

    # Save ingest state -- mark processed files
    for fpath, mtime in filtered_files:
        ingest_state[fpath] = mtime
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(ingest_state, indent=2))

    logger.info(f"Ingest complete: {stats}")
    return stats
