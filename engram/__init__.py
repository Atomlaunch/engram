"""Engram — Jarvis persistent memory system."""

__version__ = "1.3.0"

from .provider import EngramMemoryProvider
from .dialectical import (
    archive_raw_session,
    synthesize_sessions,
    apply_synthesis,
    synthesize_recall,
    run_dream,
)
from .recall import recall
from .continuity import (
    find_related_sessions,
    build_continuity_thread,
    prefetch_continuity,
)

__all__ = [
    "EngramMemoryProvider",
    "archive_raw_session",
    "synthesize_sessions",
    "apply_synthesis",
    "synthesize_recall",
    "run_dream",
    "recall",
    "find_related_sessions",
    "build_continuity_thread",
    "prefetch_continuity",
    "__version__",
]
