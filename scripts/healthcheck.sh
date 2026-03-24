#!/usr/bin/env bash
# Engram System Health Check — end-to-end diagnostics
# Runs 4x/day via OpenClaw cron; posts report to Discord #logs channel

# Resolve workspace root: prefer ENGRAM_WORKSPACE env var, then walk up from this script
if [ -n "$ENGRAM_WORKSPACE" ]; then
  WORKSPACE="$ENGRAM_WORKSPACE"
else
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  WORKSPACE="$(cd "$SCRIPT_DIR/../.." && pwd)"
fi
PYTHON="${ENGRAM_PYTHON:-$WORKSPACE/.venv-memory/bin/python}"
ENGRAM="$WORKSPACE/engram"
PASS="✅"
FAIL="❌"
WARN="⚠️"

REPORT_LINES=()
OVERALL_OK=true

# ── 1. Ollama / qwen3.5:9b ────────────────────────────────────────────────────
# Check if auto-start is enabled in engram/config.json (opt-in, default: false)
OLLAMA_AUTO_START=$(python3 -c "
import json
try:
    cfg = json.load(open('$ENGRAM/config.json'))
    print('true' if cfg.get('ollama_auto_start', False) else 'false')
except:
    print('false')
" 2>/dev/null)

if curl -sf --connect-timeout 3 http://127.0.0.1:11434/api/tags > /tmp/engram-hc-tags.json 2>&1; then
  if python3 -c "import json,sys; d=json.load(open('/tmp/engram-hc-tags.json')); names=[m['name'] for m in d.get('models',[])]; sys.exit(0 if any('qwen3.5' in n for n in names) else 1)" 2>/dev/null; then
    REPORT_LINES+=("$PASS **Ollama** — running, qwen3.5:9b loaded")
  else
    REPORT_LINES+=("$WARN **Ollama** — running but qwen3.5:9b not loaded")
    OVERALL_OK=false
  fi
else
  OVERALL_OK=false
  if [[ "$OLLAMA_AUTO_START" == "true" ]]; then
    REPORT_LINES+=("$WARN **Ollama** — not running, auto-starting (ollama_auto_start=true)...")
    ollama serve > /tmp/ollama.log 2>&1 &
    sleep 5
    if curl -sf --connect-timeout 3 http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
      REPORT_LINES+=("$PASS **Ollama** — auto-started successfully")
      OVERALL_OK=true
    else
      REPORT_LINES+=("$FAIL **Ollama** — failed to auto-start (check /tmp/ollama.log)")
    fi
  else
    REPORT_LINES+=("$FAIL **Ollama** — not running (set ollama_auto_start=true in config.json to enable auto-start)")
  fi
fi

# ── 2. Neo4j — use engram's own connection logic ──────────────────────────────
NEO4J_OUT=$($PYTHON -c "
import json, sys
sys.path.insert(0, '$WORKSPACE')
cfg = json.load(open('$ENGRAM/config.json'))
neo4j_cfg = cfg.get('neo4j', {})
uri = neo4j_cfg.get('uri', 'bolt://localhost:7687')
user = neo4j_cfg.get('user', 'neo4j')
pw = neo4j_cfg.get('password', 'password')
from neo4j import GraphDatabase
try:
    d = GraphDatabase.driver(uri, auth=(user, pw))
    with d.session() as s:
        r = s.run('RETURN 1 AS ok').single()
        print('ok' if r and r['ok'] == 1 else 'fail')
    d.close()
except Exception as e:
    print(f'error: {e}')
" 2>&1)

if [[ "$NEO4J_OUT" == "ok" ]]; then
  REPORT_LINES+=("$PASS **Neo4j** — bolt connection healthy")
else
  REPORT_LINES+=("$FAIL **Neo4j** — ${NEO4J_OUT:0:120}")
  OVERALL_OK=false
fi

# ── 3. Graph query path ───────────────────────────────────────────────────────
QUERY_OUT=$($PYTHON "$ENGRAM/context_query.py" query "TheDev favorite car" --agent main --limit 3 --json 2>&1)
if echo "$QUERY_OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if 'facts' in d or 'entities' in d else 1)" 2>/dev/null; then
  COUNT=$(echo "$QUERY_OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('facts',[])) + len(d.get('entities',[])))" 2>/dev/null)
  REPORT_LINES+=("$PASS **Graph query** — $COUNT result(s) for test query")
else
  REPORT_LINES+=("$FAIL **Graph query** — context_query.py query path failed")
  OVERALL_OK=false
fi

# ── 4. LLM extraction pipeline (afterTurn smoke test) ────────────────────────
SESSION_ID="healthcheck-$(date +%s)"
HC_TS=$(TZ="America/Los_Angeles" date '+%Y-%m-%d %H:%M:%S %Z')
EXTRACT_OUT=$($PYTHON "$ENGRAM/context_query.py" extract_llm \
  --text "Health check at $HC_TS: Engram healthcheck ran successfully on host $(hostname)" \
  --agent main --session "$SESSION_ID" 2>&1)

if echo "$EXTRACT_OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('ok') else 1)" 2>/dev/null; then
  STORED=$(echo "$EXTRACT_OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('stored',0))" 2>/dev/null)
  REPORT_LINES+=("$PASS **LLM extraction** — qwen3.5:9b extracted $STORED fact(s) from test input")
else
  ERR=$(echo "$EXTRACT_OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('error','unknown'))" 2>/dev/null || echo "${EXTRACT_OUT:0:100}")
  REPORT_LINES+=("$FAIL **LLM extraction** — $ERR")
  OVERALL_OK=false
fi

# ── 5. Pinned facts ───────────────────────────────────────────────────────────
PINNED_OUT=$($PYTHON "$ENGRAM/context_query.py" pinned --agent main --limit 5 2>&1)
if echo "$PINNED_OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if 'facts' in d else 1)" 2>/dev/null; then
  PCOUNT=$(echo "$PINNED_OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('facts',[])))" 2>/dev/null)
  REPORT_LINES+=("$PASS **Pinned facts** — $PCOUNT pinned fact(s) active")
else
  REPORT_LINES+=("$WARN **Pinned facts** — query returned unexpected output")
fi

# ── 6. Graph stats ────────────────────────────────────────────────────────────
STATS_OUT=$($PYTHON -c "
import json, sys
sys.path.insert(0, '$WORKSPACE')
cfg = json.load(open('$ENGRAM/config.json'))
neo4j_cfg = cfg.get('neo4j', {})
uri = neo4j_cfg.get('uri', 'bolt://localhost:7687')
user = neo4j_cfg.get('user', 'neo4j')
pw = neo4j_cfg.get('password', 'password')
from neo4j import GraphDatabase
d = GraphDatabase.driver(uri, auth=(user, pw))
with d.session() as s:
    nodes = s.run('MATCH (n) RETURN count(n) AS c').single()['c']
    rels = s.run('MATCH ()-[r]->() RETURN count(r) AS c').single()['c']
    facts = s.run('MATCH (n:Fact) RETURN count(n) AS c').single()['c']
    print(f'{nodes:,} nodes | {rels:,} relationships | {facts:,} facts')
d.close()
" 2>&1)

if [[ "$STATS_OUT" =~ nodes ]]; then
  REPORT_LINES+=("$PASS **Graph stats** — $STATS_OUT")
else
  REPORT_LINES+=("$WARN **Graph stats** — unavailable")
fi

# ── Build report ──────────────────────────────────────────────────────────────
TIMESTAMP=$(TZ="America/Los_Angeles" date '+%Y-%m-%d %H:%M %Z')
if $OVERALL_OK; then
  HEADER="## 🟢 Engram Health — $TIMESTAMP"
  FOOTER="_All systems operational._"
else
  HEADER="## 🔴 Engram Health — $TIMESTAMP"
  FOOTER="_⚠️ One or more systems need attention — check logs._"
fi

BODY=$(printf '%s\n' "${REPORT_LINES[@]}")
printf '%s\n\n%s\n\n%s\n' "$HEADER" "$BODY" "$FOOTER"
