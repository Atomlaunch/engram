#!/usr/bin/env bash
# Engram post-update safety check
# Run this after any hermes update to verify everything is still wired.
# Usage: bash ~/engram-dev/scripts/post-update.sh

set -uo pipefail

PASS=0
FAIL=0
WARN=0

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}✗${NC} $1"; FAIL=$((FAIL+1)); }
warn() { echo -e "  ${YELLOW}!${NC} $1"; WARN=$((WARN+1)); }

echo ""
echo "Engram post-update check"
echo "========================"
echo ""

# --- 1. Engram binary ---
echo "1. Engram binary"
if command -v engram &>/dev/null; then
    VER=$(engram version 2>/dev/null | head -1)
    ok "engram found: $VER"
else
    fail "engram binary not found -- run: pip install -e ~/engram-dev/ --break-system-packages"
fi

# --- 2. Vault ---
echo ""
echo "2. Obsidian vault"
VAULT=$(python3 -c "
import json, pathlib
cfg = pathlib.Path('~/.engram/config.json').expanduser()
if cfg.exists():
    print(json.loads(cfg.read_text()).get('vault_path', '~/obsidian-vault'))
else:
    print('~/obsidian-vault')
" 2>/dev/null || echo "~/obsidian-vault")
VAULT_EXPANDED=$(eval echo "$VAULT")

if [ -d "$VAULT_EXPANDED" ]; then
    NOTE_COUNT=$(find "$VAULT_EXPANDED" -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
    ok "vault found: $VAULT_EXPANDED ($NOTE_COUNT notes)"
else
    fail "vault not found at $VAULT_EXPANDED -- run: engram init"
fi

# --- 3. Engram config ---
echo ""
echo "3. Engram config"
CONFIG="$HOME/.engram/config.json"
if [ -f "$CONFIG" ]; then
    ok "config found: $CONFIG"
else
    warn "no config at $CONFIG -- using defaults (run: engram init to create)"
fi

# --- 4. Index DB ---
echo ""
echo "4. Index database"
DB="$HOME/.engram/vault_index.db"
if [ -f "$DB" ]; then
    SIZE=$(du -sh "$DB" 2>/dev/null | cut -f1)
    ok "index DB found: $DB ($SIZE)"
else
    warn "index DB missing -- run: engram index"
fi

# --- 5. Hermes config + plugin shim ---
echo ""
echo "5. Hermes integration"
HERMES_CFG="$HOME/.hermes/config.yaml"
SHIM_DIR="$HOME/.hermes/hermes-agent/plugins/memory/engram"
PLUGIN_DIR="$HOME/.hermes/plugins/engram"

if [ -f "$HERMES_CFG" ]; then
    if grep -q "prefill_messages_file.*engram" "$HERMES_CFG" 2>/dev/null; then
        ok "prefill_messages_file set in config.yaml"
    fi
    if grep -q "provider.*engram" "$HERMES_CFG" 2>/dev/null; then
        ok "memory.provider: engram set in config.yaml"
    fi
else
    fail "hermes config not found at $HERMES_CFG"
fi

# Check stable plugin location
if [ -d "$PLUGIN_DIR" ]; then
    ok "engram plugin found at ~/.hermes/plugins/engram/"
else
    warn "engram plugin dir missing -- run: cp -r ~/engram-dev/plugin ~/.hermes/plugins/engram"
fi

# Check shim -- restore if wiped by hermes update
if [ -f "$SHIM_DIR/__init__.py" ]; then
    ok "hermes-agent shim found at plugins/memory/engram/"
else
    warn "shim wiped by hermes update -- restoring..."
    mkdir -p "$SHIM_DIR"
    cp ~/engram-dev/scripts/shim/__init__.py "$SHIM_DIR/__init__.py" 2>/dev/null \
        || python3 -c "
import shutil, pathlib
src = pathlib.Path('$HOME/.hermes/plugins/engram/__init__.py')
shim = pathlib.Path('$SHIM_DIR')
shim.mkdir(parents=True, exist_ok=True)
# Write minimal shim
(shim / '__init__.py').write_text('''
import sys
from pathlib import Path
_DIR = Path.home() / \".hermes\" / \"plugins\"
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))
try:
    from engram import EngramMemoryProvider
    def register(ctx): ctx.register_memory_provider(EngramMemoryProvider())
except ImportError as e:
    import logging; logging.getLogger(__name__).warning(\"Engram: %s\", e)
''')
" && ok "shim restored" || fail "shim restore failed -- run manually"
fi

# --- 6. Briefing file ---
echo ""
echo "6. Session briefing"
BRIEFING="$HOME/.hermes/engram-briefing.json"
if [ -f "$BRIEFING" ]; then
    # Check age
    if [ "$(uname)" = "Darwin" ]; then
        AGE=$(( $(date +%s) - $(stat -f %m "$BRIEFING") ))
    else
        AGE=$(( $(date +%s) - $(stat -c %Y "$BRIEFING") ))
    fi
    HOURS=$(( AGE / 3600 ))
    SIZE=$(wc -c < "$BRIEFING" | tr -d ' ')

    if [ "$HOURS" -gt 24 ]; then
        warn "briefing is ${HOURS}h old (${SIZE} bytes) -- regenerating..."
        python3 ~/engram-dev/scripts/gen-briefing.py --reindex 2>/dev/null && ok "briefing regenerated" || fail "briefing regeneration failed"
    else
        ok "briefing fresh: ${HOURS}h old, ${SIZE} bytes"
    fi
else
    warn "no briefing file -- generating..."
    python3 ~/engram-dev/scripts/gen-briefing.py --reindex 2>/dev/null && ok "briefing generated" || fail "briefing generation failed"
fi

# --- 7. Cron jobs ---
echo ""
echo "7. Cron jobs"
# Check via hermes cron list if available
CRON_OUT=$(hermes cron list 2>/dev/null || echo "")
if echo "$CRON_OUT" | grep -q "engram-daily"; then
    ok "engram-daily cron found"
else
    warn "engram-daily cron not found -- may need to recreate after update"
fi

# --- 8. Python deps ---
echo ""
echo "8. Python dependencies"
python3 -c "import frontmatter; import watchdog; import click; import anthropic" 2>/dev/null \
    && ok "all python deps available (system python)" \
    || { fail "missing python deps -- run: pip install -e ~/engram-dev/ --break-system-packages"; }

# Also ensure engram is installed in the hermes venv (gateway uses it)
HERMES_PY="$HOME/.hermes/hermes-agent/venv/bin/python3"
if [ -f "$HERMES_PY" ]; then
    if "$HERMES_PY" -c "import engram" 2>/dev/null; then
        ok "engram installed in hermes venv"
    else
        warn "engram not in hermes venv -- installing..."
        cd "$HOME/.hermes/hermes-agent" && uv pip install \
            --python "$HERMES_PY" -e "$HOME/engram-dev/" -q 2>/dev/null \
            && ok "engram installed in hermes venv" \
            || fail "failed to install engram in hermes venv"
    fi
fi

# --- 9. Quick smoke test ---
echo ""
echo "9. Smoke test"
if engram version &>/dev/null; then
    STATS=$(engram version 2>/dev/null | grep "Total notes" || echo "")
    if [ -n "$STATS" ]; then
        ok "engram version: $STATS"
    else
        ok "engram version: OK"
    fi
else
    fail "engram version failed"
fi

# --- Summary ---
echo ""
echo "========================"
echo -e "Results: ${GREEN}${PASS} passed${NC}  ${YELLOW}${WARN} warnings${NC}  ${RED}${FAIL} failed${NC}"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "Action required -- fix the failures above before starting a session."
    exit 1
elif [ "$WARN" -gt 0 ]; then
    echo "Warnings present but Engram should still work. Review above."
    exit 0
else
    echo "All good. Engram is ready."
    exit 0
fi
