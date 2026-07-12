#!/usr/bin/env bash
# Lector setup — verifies dependencies, installs the /lector skill for Claude
# Code, and starts the local server. Idempotent: safe to run repeatedly.
#
#   ./setup.sh           check deps, install skill, start server (localhost)
#   ./setup.sh --lan     same, but reachable from phone/tablet on your network
#   ./setup.sh --stop    stop the server
#   ./setup.sh --status  show server status and URLs

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${LECTOR_PORT:-8123}"

status() {
  if PID=$(lsof -ti tcp:"$PORT" 2>/dev/null); then
    echo "✓ Server running (pid $PID) → http://127.0.0.1:$PORT"
    if command -v ipconfig >/dev/null 2>&1; then
      IP=$(ipconfig getifaddr en0 2>/dev/null || true)
      [ -n "${IP:-}" ] && echo "  LAN (if started with --lan): http://$IP:$PORT"
    fi
  else
    echo "○ Server not running"
  fi
}

case "${1:-}" in
  --stop)
    if PID=$(lsof -ti tcp:"$PORT" 2>/dev/null); then kill "$PID" && echo "✓ Server stopped"; else echo "○ Nothing running on port $PORT"; fi
    exit 0 ;;
  --status)
    status; exit 0 ;;
esac

echo "── Lector setup ─────────────────────────────"

# 1. Dependencies ------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || { echo "✗ python3 not found — install Python 3.9+"; exit 1; }
echo "✓ python3 $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"

if command -v claude >/dev/null 2>&1; then
  echo "✓ claude CLI $(claude --version 2>/dev/null | head -1)"
else
  echo "! claude CLI not found — the reader works without it, but all AI"
  echo "  features (explanations, chat, agent) need it:"
  echo "    npm install -g @anthropic-ai/claude-code   # then: claude login"
fi

# 2. Install the /lector skill for Claude Code -------------------------------
# Symlink, not copy: one source of truth, repo updates apply immediately.
mkdir -p "$HOME/.claude/skills"
ln -sfn "$ROOT/.claude/skills/lector" "$HOME/.claude/skills/lector"
echo "✓ Skill installed: ~/.claude/skills/lector → $ROOT/.claude/skills/lector"

# 3. Start the server ---------------------------------------------------------
HOST="127.0.0.1"
[ "${1:-}" = "--lan" ] && HOST="0.0.0.0"

if lsof -ti tcp:"$PORT" >/dev/null 2>&1; then
  echo "✓ Server already running on port $PORT (./setup.sh --stop to restart)"
else
  LECTOR_HOST="$HOST" nohup python3 "$ROOT/server.py" > "$ROOT/server.log" 2>&1 &
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.4
    curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/books" && break
  done
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/books" \
    || { echo "✗ Server failed to start — see server.log"; exit 1; }
  echo "✓ Server started ($HOST:$PORT)"
fi

echo "─────────────────────────────────────────────"
status
echo
echo "Next: open the URL above, or tell Claude Code: \"lies mit\" / \"read along\"."
