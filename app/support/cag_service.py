"""CAG = preload one consolidated context blob, cache it with a TTL, serve it straight from memory (no retrieval, no generation).
Single-process in-memory store; move to Redis if you run several workers."""
from __future__ import annotations
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from ..models import aware, now
from .config import cfg
from .rag_service import RagDocument, collect_documents

MAX_CONTEXT_CHARS = 200_000


@dataclass
class CagCache:
    context_text: str = ""
    document_count: int = 0
    by_type: dict = field(default_factory=dict)
    built_at: Optional[datetime] = None
    ttl: timedelta = field(default_factory=lambda: timedelta(minutes=30))

    def is_stale(self) -> bool:
        return self.built_at is None or now() - aware(self.built_at) > self.ttl


_lock = threading.Lock()
_cache = CagCache()


def _context(documents: list[RagDocument]) -> str:
    parts, total = [], 0
    for d in documents:
        line = f"[{d.doc_type}:{d.doc_id}] {d.title} - {d.text}"
        if total + len(line) > MAX_CONTEXT_CHARS:
            break
        parts.append(line)
        total += len(line)
    return "\n".join(parts)


def refresh_cache(db: Session) -> dict:
    global _cache
    docs = collect_documents(db)
    by_type: dict[str, int] = {}
    for d in docs:
        by_type[d.doc_type] = by_type.get(d.doc_type, 0) + 1
    with _lock:
        _cache = CagCache(_context(docs), len(docs), by_type, now(), timedelta(minutes=cfg.cag_ttl_minutes))
    return summary()


def get_context(max_chars: Optional[int] = None) -> dict:
    with _lock:
        c = _cache
    return {"context": c.context_text[:max_chars] if max_chars is not None else c.context_text, "document_count": c.document_count,
            "by_type": c.by_type, "built_at": c.built_at, "stale": c.is_stale()}


def summary() -> dict:
    with _lock:
        c = _cache
    return {"document_count": c.document_count, "by_type": c.by_type, "built_at": c.built_at, "stale": c.is_stale(), "context_size_chars": len(c.context_text)}
