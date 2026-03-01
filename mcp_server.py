#!/usr/bin/env python3
"""
Engram MCP Server — Memory-as-a-Service for AI Agents

Exposes Engram's temporal knowledge graph via Model Context Protocol.
Agents can search memories, query entities, get context, and more.

Usage:
  python mcp_server.py  # Run as MCP server (stdio transport)
  mcp dev mcp_server.py # Dev mode with Inspector UI
"""

from typing import Any
import sys
import os
from pathlib import Path

# Add parent directory to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server.fastmcp import FastMCP

# Initialize MCP server
mcp = FastMCP("engram-memory")


@mcp.tool()
async def search_memory(query: str, limit: int = 10, agent_id: str = None) -> str:
    """
    Search across all memory types (entities, facts, episodes, emotions).
    
    Args:
        query: Search query (natural language or keywords)
        limit: Maximum number of results to return (default: 10)
        agent_id: Optional agent scope filter (returns agent + shared results)
    
    Returns:
        Formatted search results with relevance scores
    """
    from query import unified_search
    from schema import get_db, get_conn
    
    try:
        db = get_db(read_only=True)
        conn = get_conn(db)
        results = unified_search(conn, query, limit=limit, agent_id=agent_id or None)
        
        if not results:
            return f"No results found for query: {query}"
        
        output = []
        output.append(f"Found {len(results)} results for '{query}':\n")
        
        for i, result in enumerate(results, 1):
            output.append(f"{i}. [{result['type']}] Score: {result['score']:.2f}")
            output.append(f"   {result['text']}")
            if result.get('timestamp'):
                output.append(f"   Time: {result['timestamp']}")
            output.append("")
        
        return "\n".join(output)
    except Exception as e:
        return f"Error searching memory: {str(e)}"


@mcp.tool()
async def get_entity_context(name: str) -> str:
    """
    Get full context for a specific entity (person, place, concept, etc.).
    
    Args:
        name: Name of the entity (e.g., "The Dev", "OpenClaw", "MorningClaw")
    
    Returns:
        Complete entity context including facts, relationships, episodes, and emotions
    """
    from query import get_entity_context as get_ctx
    from schema import get_db, get_conn
    
    try:
        db = get_db(read_only=True)
        conn = get_conn(db)
        context = get_ctx(conn, name)
        
        if not context:
            return f"Entity not found: {name}"
        
        output = []
        output.append(f"=== Entity: {context['entity']['name']} ===")
        output.append(f"Type: {context['entity']['type']}")
        if context['entity'].get('description'):
            output.append(f"Description: {context['entity']['description']}")
        output.append("")
        
        if context.get('facts'):
            output.append(f"Facts ({len(context['facts'])}):")
            for fact in context['facts']:
                output.append(f"  • {fact['fact']}")
            output.append("")
        
        if context.get('relationships'):
            output.append(f"Relationships ({len(context['relationships'])}):")
            for rel in context['relationships']:
                output.append(f"  • {rel['type']}: {rel['target']}")
                if rel.get('details'):
                    output.append(f"    ({rel['details']})")
            output.append("")
        
        if context.get('episodes'):
            output.append(f"Recent Episodes ({len(context['episodes'])}):")
            for ep in context['episodes'][:5]:  # Limit to 5 most recent
                output.append(f"  • {ep['timestamp']}: {ep['description']}")
            output.append("")
        
        if context.get('emotions'):
            output.append(f"Emotions ({len(context['emotions'])}):")
            for emotion in context['emotions'][:3]:
                output.append(f"  • {emotion['emotion']} (intensity: {emotion.get('intensity', 'N/A')})")
            output.append("")
        
        return "\n".join(output)
    except Exception as e:
        return f"Error getting entity context: {str(e)}"


@mcp.tool()
async def get_session_briefing() -> str:
    """
    Generate a session startup briefing with recent memories and context.
    
    Returns:
        Markdown-formatted briefing with key entities, recent events, and priorities
    """
    from briefing import generate_briefing
    from schema import get_db, get_conn
    
    try:
        db = get_db(read_only=True)
        conn = get_conn(db)
        briefing = generate_briefing(conn)
        return briefing
    except Exception as e:
        return f"Error generating briefing: {str(e)}"


@mcp.tool()
async def get_memory_stats() -> str:
    """
    Get statistics about the knowledge graph (entity counts, relationship types, etc.).
    
    Returns:
        Formatted statistics about the memory graph
    """
    from schema import get_db, get_conn, get_stats
    
    try:
        db = get_db(read_only=True)
        conn = get_conn(db)
        stats = get_stats(conn)
        
        output = []
        output.append("=== Engram Memory Statistics ===")
        output.append(f"Total Entities: {stats.get('total_entities', 0)}")
        output.append(f"Total Facts: {stats.get('total_facts', 0)}")
        output.append(f"Total Relationships: {stats.get('total_relationships', 0)}")
        output.append(f"Total Episodes: {stats.get('total_episodes', 0)}")
        output.append(f"Total Emotions: {stats.get('total_emotions', 0)}")
        
        if stats.get('entity_types'):
            output.append("\nEntity Types:")
            for etype, count in stats['entity_types'].items():
                output.append(f"  • {etype}: {count}")
        
        if stats.get('relationship_types'):
            output.append("\nRelationship Types:")
            for rtype, count in list(stats['relationship_types'].items())[:10]:
                output.append(f"  • {rtype}: {count}")
        
        return "\n".join(output)
    except Exception as e:
        return f"Error getting stats: {str(e)}"


@mcp.tool()
async def query_recent_memories(hours: int = 24, limit: int = 20, agent_id: str = None) -> str:
    """
    Query recent memories from the last N hours.
    
    Args:
        hours: Number of hours to look back (default: 24)
        limit: Maximum number of memories to return (default: 20)
        agent_id: Optional agent scope filter (returns agent + shared results)
    
    Returns:
        Recent memories sorted by timestamp
    """
    from schema import get_db, get_conn
    
    try:
        db = get_db(read_only=True)
        conn = get_conn(db)
        
        # Query recent episodes (most useful for temporal queries)
        cursor = conn.execute("""
            SELECT timestamp, description, entities, tags
            FROM episodes
            WHERE timestamp > datetime('now', ?)
            ORDER BY timestamp DESC
            LIMIT ?
        """, (f'-{hours} hours', limit))
        
        episodes = cursor.fetchall()
        
        if not episodes:
            return f"No memories found in the last {hours} hours."
        
        output = []
        output.append(f"=== Recent Memories (last {hours} hours) ===\n")
        
        for ep in episodes:
            timestamp, description, entities, tags = ep
            output.append(f"• {timestamp}")
            output.append(f"  {description}")
            if entities:
                output.append(f"  Entities: {entities}")
            if tags:
                output.append(f"  Tags: {tags}")
            output.append("")
        
        return "\n".join(output)
    except Exception as e:
        return f"Error querying recent memories: {str(e)}"


@mcp.resource("engram://config")
def get_config_info() -> str:
    """Information about the Engram memory system."""
    return """
Engram Memory System
====================

Engram is a temporal knowledge graph for AI memory and reasoning.

Features:
- Entity tracking (people, places, concepts, tools)
- Relationship mapping
- Episode recording (temporal events)
- Fact storage
- Emotion tracking

Database: Kùzu embedded graph database
Location: .engram-db (or ENGRAM_DB_PATH env var)

Usage:
- search_memory(): Full-text search across all memory types
- get_entity_context(): Deep context for specific entities
- get_session_briefing(): Session startup summary
- query_recent_memories(): Time-based queries
- get_memory_stats(): Graph statistics

Architecture: Designed for agentic memory persistence and knowledge sharing.
"""


@mcp.prompt()
def analyze_entity_relationships(entity_name: str) -> str:
    """Template: Analyze an entity's relationships and context."""
    return f"""Analyze the entity "{entity_name}" from the Engram memory graph.

1. Use get_entity_context("{entity_name}") to retrieve full context
2. Identify key relationships and their significance
3. Note any patterns or trends in associated episodes
4. Summarize the entity's role in the overall knowledge graph

Provide a structured analysis with insights."""


def main():
    """Run the MCP server."""
    import sys
    
    # Log to stderr for debugging (stdout is reserved for MCP protocol)
    print("Starting Engram MCP Server...", file=sys.stderr)
    print("Available tools: search_memory, get_entity_context, get_session_briefing, get_memory_stats, query_recent_memories", file=sys.stderr)
    
    # Run with stdio transport (default for MCP)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
