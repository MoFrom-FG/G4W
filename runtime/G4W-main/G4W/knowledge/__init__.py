"""G4W Knowledge Base package.

Knowledge is intentionally separated from conversation memory. This package
stores user-supplied documents and search indexes under the knowledge roots
only; callers must not write document content into long-term memory.
"""

from .commands import handle_kb_command
from .store import KnowledgeStore

__all__ = [
    "KnowledgeStore",
    "handle_kb_command",
    "ingest_document",
    "search_knowledge",
]


def __getattr__(name: str):
    if name == "ingest_document":
        from .ingest import ingest_document

        return ingest_document
    if name == "search_knowledge":
        from .search import search_knowledge

        return search_knowledge
    raise AttributeError(name)
