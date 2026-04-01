#!/usr/bin/env python3
"""
Goal Tracking for Persistent AGI Memory
Manages long-term goals and stale goal detection.
"""

import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .schema import get_conn, init_schema


class GoalTracker:
    def __init__(self):
        self.conn = get_conn()
        init_schema(self.conn)

    def create_goal(self, title: str, time_horizon: str = "30day", 
                   importance: float = 0.8, agent_id: str = "main") -> str:
        """Create a new goal."""
        goal_id = f"goal_{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat()
        
        query = """
        CREATE (g:Goal {
            id: $id,
            title: $title,
            status: 'active',
            timeHorizon: $time_horizon,
            lastActivity: $now,
            importance: $importance,
            agent_id: $agent_id,
            created_at: $now,
            updated_at: $now
        })
        RETURN g.id as id
        """
        
        self.conn.execute(query, {
            "id": goal_id,
            "title": title,
            "time_horizon": time_horizon,
            "importance": importance,
            "now": now,
            "agent_id": agent_id
        })
        
        print(f"✅ Created goal: {title} ({goal_id})")
        return goal_id

    def mark_activity(self, goal_id: str):
        """Update last activity timestamp."""
        now = datetime.now().isoformat()
        query = """
        MATCH (g:Goal WHERE g.id = $id)
        SET g.lastActivity = $now, g.updated_at = $now
        """
        self.conn.execute(query, {"id": goal_id, "now": now})

    def get_stale_goals(self, agent_id: str = "main", days: int = 7) -> List[Dict]:
        """Find goals with no activity."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        
        query = """
        MATCH (g:Goal)
        WHERE g.agent_id = $agent_id 
          AND g.status = 'active'
          AND (g.lastActivity < $cutoff OR g.lastActivity IS NULL)
        RETURN g
        ORDER BY g.importance DESC
        """
        
        result = self.conn.execute(query, {"agent_id": agent_id, "cutoff": cutoff})
        goals = []
        
        while result.has_next():
            row = result.get_next()
            goals.append(dict(row[0]))
            
        return goals

    def get_goal_context(self, agent_id: str = "main") -> str:
        """Compact context for model injection."""
        stale = self.get_stale_goals(agent_id, days=10)
        if not stale:
            return ""
            
        lines = ["## Stale Goals (needs attention):"]
        for g in stale[:4]:
            days_stale = "unknown"
            if g.get('lastActivity'):
                days_stale = "many"
            lines.append(f"- **{g.get('title')}** (stale for {days_stale} days)")
            
        return "\n".join(lines)


# Singleton
goal_tracker = GoalTracker()
