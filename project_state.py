#!/usr/bin/env python3
"""
Project State Management for Persistent AGI Memory
Handles structured project tracking in Engram.
"""

import uuid
from datetime import datetime
from typing import Dict, List, Optional
import json

from .schema import get_conn, init_schema


class ProjectState:
    def __init__(self):
        self.conn = get_conn()
        init_schema(self.conn)

    def create_project(self, name: str, description: str = "", priority: int = 5, 
                      next_action: str = "", agent_id: str = "main") -> str:
        """Create a new project."""
        project_id = f"proj_{uuid.uuid4().hex[:12]}"
        now = datetime.now()
        
        query = """
        CREATE (p:Project {
            id: $id,
            name: $name,
            status: 'active',
            priority: $priority,
            description: $description,
            nextAction: $next_action,
            lastUpdated: $now,
            created_at: $now,
            updated_at: $now,
            agent_id: $agent_id,
            importance: 0.7
        })
        RETURN p.id as id
        """
        
        result = self.conn.execute(query, {
            "id": project_id,
            "name": name,
            "priority": priority,
            "description": description,
            "next_action": next_action,
            "now": now,
            "agent_id": agent_id
        })
        
        print(f"✅ Created project: {name} ({project_id})")
        return project_id

    def update_project(self, project_id: str, **kwargs):
        """Update project fields."""
        now = datetime.now().isoformat()
        set_clauses = []
        params = {"id": project_id, "now": now}
        
        for key, value in kwargs.items():
            if key in ["status", "nextAction", "description", "blockedBy", "successCriteria"]:
                set_clauses.append(f"p.{key} = ${key}")
                params[key] = value
            elif key == "priority":
                set_clauses.append(f"p.priority = $priority")
                params["priority"] = value
        
        if not set_clauses:
            return False
            
        set_clauses.append("p.updated_at = $now")
        set_clause = "SET " + ", ".join(set_clauses)
        
        query = f"""
        MATCH (p:Project WHERE p.id = $id)
        {set_clause}
        RETURN p.id
        """
        
        self.conn.execute(query, params)
        print(f"✅ Updated project {project_id}")
        return True

    def get_active_projects(self, agent_id: str = "main", limit: int = 10) -> List[Dict]:
        """Get all active projects."""
        query = """
        MATCH (p:Project)
        WHERE p.agent_id = $agent_id AND (p.status = 'active' OR p.status IS NULL)
        RETURN p
        ORDER BY p.priority DESC, p.lastUpdated DESC
        LIMIT $limit
        """
        
        result = self.conn.execute(query, {"agent_id": agent_id, "limit": limit})
        projects = []
        
        while result.has_next():
            row = result.get_next()
            node = row[0]
            projects.append(dict(node))
            
        return projects

    def get_project_context(self, agent_id: str = "main") -> str:
        """Return compact context string for model injection."""
        projects = self.get_active_projects(agent_id, limit=5)
        if not projects:
            return ""
            
        lines = ["## Active Projects:"]
        for p in projects:
            status = p.get('status', 'active')
            next_action = p.get('nextAction', 'No next action defined')
            lines.append(f"- **{p.get('name')}** ({status}): {next_action}")
            
        return "\n".join(lines)


# Singleton
project_state = ProjectState()
