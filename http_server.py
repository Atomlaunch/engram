#!/usr/bin/env python3
"""
Engram HTTP Server — Read-only REST API for Engram memory system.

Exposes the same 5 tools as the MCP server via HTTP endpoints.
Read-only — no writes through HTTP (yet).

Environment:
  ENGRAM_DB_PATH  — path to the Kuzu database (default: .engram-db in this dir)
  ENGRAM_PORT     — port to listen on (default: 3456)

Usage:
  python engram/http_server.py
  ENGRAM_PORT=8080 python engram/http_server.py
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Add parent directory to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

PORT = int(os.environ.get("ENGRAM_PORT", 3456))


def _get_conn(read_only=True):
    from schema import get_db, get_conn
    db = get_db(read_only=read_only)
    return get_conn(db)


def _json(data) -> bytes:
    return json.dumps(data, default=str).encode("utf-8")


def handle_health():
    from schema import get_db, get_conn, get_stats
    try:
        db = get_db(read_only=True)
        conn = get_conn(db)
        stats = get_stats(conn)
        nodes = sum(stats.get(t, 0) for t in ["Entity", "Episode", "Emotion", "SessionState", "Fact"])
        rels = sum(v for k, v in stats.items() if k not in ["Entity", "Episode", "Emotion", "SessionState", "Fact"])
        return 200, {"ok": True, "nodes": nodes, "relationships": rels}
    except Exception as e:
        return 500, {"ok": False, "error": str(e)}


def handle_search(body: dict):
    from query import unified_search
    query = body.get("query", "")
    limit = int(body.get("limit", 10))
    agent_id = body.get("agent_id") or None
    if not query:
        return 400, {"error": "query is required"}
    try:
        conn = _get_conn()
        results = unified_search(conn, query, limit=limit, agent_id=agent_id)
        return 200, results
    except Exception as e:
        return 500, {"error": str(e)}


def handle_entity(body: dict):
    from query import get_entity_context
    name = body.get("name", "")
    if not name:
        return 400, {"error": "name is required"}
    try:
        conn = _get_conn()
        context = get_entity_context(conn, name)
        return 200, context
    except Exception as e:
        return 500, {"error": str(e)}


def handle_briefing():
    try:
        import briefing
        conn = _get_conn()
        text = briefing.generate_briefing(conn)
        return 200, {"briefing": text}
    except Exception as e:
        # Fallback: just return recent stats
        try:
            from schema import get_db, get_conn, get_stats
            db = get_db(read_only=True)
            conn = get_conn(db)
            stats = get_stats(conn)
            return 200, {"briefing": f"Engram stats: {json.dumps(stats, default=str)}"}
        except Exception as e2:
            return 500, {"error": str(e2)}


def handle_recent(body: dict):
    hours = int(body.get("hours", 24))
    limit = int(body.get("limit", 20))
    agent_id = body.get("agent_id") or None
    try:
        import kuzu
        from schema import get_db, get_conn
        from datetime import datetime, timedelta
        db = get_db(read_only=True)
        conn = get_conn(db)
        cutoff = datetime.now() - timedelta(hours=hours)

        if agent_id:
            agent_filter = " AND (ep.agent_id = $p_agent OR ep.agent_id = 'shared')"
            params = {"p_cutoff": cutoff, "p_lim": limit, "p_agent": agent_id}
        else:
            agent_filter = ""
            params = {"p_cutoff": cutoff, "p_lim": limit}

        result = conn.execute(
            "MATCH (ep:Episode) "
            "WHERE ep.occurred_at >= $p_cutoff"
            + agent_filter +
            " RETURN ep.id, ep.summary, ep.source_file, ep.occurred_at, ep.importance "
            "ORDER BY ep.occurred_at DESC LIMIT $p_lim",
            params
        )
        episodes = []
        while result.has_next():
            row = result.get_next()
            episodes.append({
                "id": row[0], "summary": row[1], "source_file": row[2],
                "occurred_at": str(row[3]), "importance": row[4]
            })
        return 200, {"hours": hours, "count": len(episodes), "episodes": episodes}
    except Exception as e:
        return 500, {"error": str(e)}


def handle_stats():
    from schema import get_db, get_conn, get_stats
    try:
        db = get_db(read_only=True)
        conn = get_conn(db)
        stats = get_stats(conn)
        return 200, stats
    except Exception as e:
        return 500, {"error": str(e)}


class EngramHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quiet logging — just method + path + status
        print(f"[engram-http] {self.command} {self.path} → {args[1] if len(args) > 1 else '?'}")

    def send_json(self, status: int, data: dict):
        body = _json(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/health":
            status, data = handle_health()
        elif path == "/briefing":
            status, data = handle_briefing()
        elif path == "/stats":
            status, data = handle_stats()
        else:
            status, data = 404, {"error": f"Not found: {path}"}

        self.send_json(status, data)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self.read_body()

        if path == "/search":
            status, data = handle_search(body)
        elif path == "/entity":
            status, data = handle_entity(body)
        elif path == "/recent":
            status, data = handle_recent(body)
        else:
            status, data = 404, {"error": f"Not found: {path}"}

        self.send_json(status, data)


def main():
    # Run schema migration on startup (safe/idempotent)
    try:
        from schema import get_db, get_conn, init_schema
        db = get_db(read_only=False)
        conn = get_conn(db)
        init_schema(conn)
        print(f"✅ Engram schema ready")
    except Exception as e:
        print(f"⚠️  Schema init warning: {e}")

    server = HTTPServer(("0.0.0.0", PORT), EngramHandler)
    print(f"🧠 Engram HTTP server listening on port {PORT}")
    print(f"   GET  /health    — system health")
    print(f"   POST /search    — search memories")
    print(f"   POST /entity    — entity context")
    print(f"   GET  /briefing  — session briefing")
    print(f"   POST /recent    — recent memories")
    print(f"   GET  /stats     — graph statistics")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Engram HTTP server stopped")


if __name__ == "__main__":
    main()
