"""RAG = retrieval + display only (no generation). COLLECT store orders / support tickets / worklogs as small text documents, then
RETRIEVE the best matches for a free-text query with TF-IDF cosine similarity. scikit-learn is imported lazily so the shop still
boots if it isn't installed (the /rag endpoints then answer 503). Swap TF-IDF for embeddings later without changing the API."""
from __future__ import annotations
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import Order, now
from .models import Ticket, Worklog


class RagUnavailable(RuntimeError):
    pass


@dataclass
class RagDocument:
    doc_id: str
    doc_type: str          # order | ticket | worklog
    title: str
    text: str
    metadata: dict = field(default_factory=dict)


class _RagIndex:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.documents: list[RagDocument] = []
        self.vectorizer = None
        self.matrix = None
        self.built_at: Optional[datetime] = None

    def build(self, documents: list[RagDocument]) -> None:
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
        except ImportError as e:
            raise RagUnavailable("scikit-learn is not installed (add scikit-learn to requirements.txt)") from e
        with self._lock:
            self.documents = documents
            self.vectorizer = self.matrix = None
            if documents:
                self.vectorizer = TfidfVectorizer(stop_words="english", max_features=5000)
                self.matrix = self.vectorizer.fit_transform([d.text for d in documents])
            self.built_at = now()

    def search(self, query: str, top_k: int) -> list[tuple[RagDocument, float]]:
        with self._lock:
            if not self.documents or self.vectorizer is None:
                return []
            import numpy as np
            from sklearn.metrics.pairwise import cosine_similarity
            scores = cosine_similarity(self.vectorizer.transform([query]), self.matrix).flatten()
            return [(self.documents[i], float(scores[i])) for i in np.argsort(scores)[::-1][:top_k] if scores[i] > 0]


_index = _RagIndex()


def collect_documents(db: Session, limit: int = 5000) -> list[RagDocument]:
    docs: list[RagDocument] = []
    for o in db.scalars(select(Order).order_by(Order.id.desc()).limit(limit)):
        items = ", ".join(f"{i.title} x{i.qty}" for i in o.items)
        docs.append(RagDocument(f"order:{o.id}", "order", f"Order {o.number}",
                                f"Order {o.number}: {items}, total {o.total_cents / 100:.2f} {o.currency.upper()}, status {o.status}, "
                                f"tracking {o.carrier or ''} {o.tracking_number or ''}.",
                                {"order_number": o.number, "status": o.status, "total_cents": o.total_cents, "user_id": o.user_id}))
    for t in db.scalars(select(Ticket).order_by(Ticket.created_at.desc()).limit(limit)):
        docs.append(RagDocument(f"ticket:{t.id}", "ticket", t.subject,
                                f"Ticket: {t.subject}. {t.description or ''} Status={t.status.value}, priority={t.priority.value}, channel={t.channel.value}, "
                                f"resolution={t.resolution_type.value if t.resolution_type else 'n/a'}, order={t.order_number or 'n/a'}.",
                                {"ticket_id": t.id, "status": t.status.value, "priority": t.priority.value, "order_number": t.order_number, "sla_breached": t.sla_breached}))
    for w in db.scalars(select(Worklog).order_by(Worklog.created_at.desc()).limit(limit)):
        docs.append(RagDocument(f"worklog:{w.id}", "worklog", f"Worklog ({w.action})",
                                f"Worklog on ticket {w.ticket_id}: actor={w.actor}, action={w.action}. {w.note or ''}",
                                {"ticket_id": w.ticket_id, "actor": w.actor, "action": w.action}))
    return docs


def _counts(documents: list[RagDocument]) -> dict:
    out: dict[str, int] = {}
    for d in documents:
        out[d.doc_type] = out.get(d.doc_type, 0) + 1
    return out


def build_index(db: Session) -> dict:
    docs = collect_documents(db)
    _index.build(docs)
    return {"documents_indexed": len(docs), "built_at": _index.built_at, "by_type": _counts(docs)}


def search(query: str, top_k: int = 5) -> list[dict]:
    return [{"doc_id": d.doc_id, "doc_type": d.doc_type, "title": d.title, "snippet": d.text[:280], "score": round(s, 4), "metadata": d.metadata}
            for d, s in _index.search(query, top_k)]


def index_status() -> dict:
    return {"documents_indexed": len(_index.documents), "built_at": _index.built_at, "by_type": _counts(_index.documents)}
