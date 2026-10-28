"""Vector index over events (ChromaDB on disk + a multilingual embedding model running locally).

    python -m teampulse.index            # embed new/changed events, drop deleted ones

The default model (paraphrase-multilingual-MiniLM-L12-v2 via FastEmbed, ~0.2 GB, downloaded once, no
PyTorch) handles Hindi, English and Hinglish. See make_embedder() for the alternatives.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from teampulse import config, db

log = logging.getLogger("teampulse.index")
CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200


# ---- embedders -------------------------------------------------------------------------------

class SentenceTransformerEmbedder:
    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_name)
        self.e5 = "e5" in model_name.lower()   # e5 models expect "query:" / "passage:" prefixes
        self.name = model_name
        # Minimum similarity to auto-link a chat message to a task (e5 scores sit high, ~0.7–0.9).
        self.link_threshold = float(config.get("TEAMPULSE_LINK_THRESHOLD", "0.84" if self.e5 else "0.6"))

    def documents(self, texts: list[str]) -> list[list[float]]:
        texts = [f"passage: {t}" for t in texts] if self.e5 else texts
        return self.model.encode(texts, normalize_embeddings=True, batch_size=32).tolist()

    def query(self, text: str) -> list[float]:
        text = f"query: {text}" if self.e5 else text
        return self.model.encode([text], normalize_embeddings=True)[0].tolist()


class FastEmbedEmbedder:
    """ONNX models via FastEmbed: no PyTorch, small install, fits free hosting. Default for TeamPulse."""

    def __init__(self, model_name: str):
        from fastembed import TextEmbedding

        self.model = TextEmbedding(model_name=model_name)
        self.name = f"fastembed:{model_name}"
        self.e5 = "e5" in model_name.lower()
        default = "0.84" if self.e5 else "0.65"   # paraphrase-multilingual scores run lower than e5
        self.link_threshold = float(config.get("TEAMPULSE_LINK_THRESHOLD", default))

    @staticmethod
    def _unit(vectors) -> list[list[float]]:
        out = []
        for v in vectors:
            norm = math.sqrt(float((v * v).sum())) or 1.0
            out.append([float(x) / norm for x in v])
        return out

    def documents(self, texts: list[str]) -> list[list[float]]:
        texts = [f"passage: {t}" for t in texts] if self.e5 else texts
        return self._unit(self.model.embed(texts, batch_size=32))

    def query(self, text: str) -> list[float]:
        return self._unit(self.model.embed([f"query: {text}" if self.e5 else text]))[0]


class HashEmbedder:
    """Bag-of-words hashing. No download, keyword matching only. For tests and offline use."""
    name = "hash"
    dims = 512
    link_threshold = 0.35

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        for word in re.findall(r"\w+", text.lower()):
            vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dims] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def documents(self, texts):
        return [self._vec(t) for t in texts]

    def query(self, text):
        return self._vec(text)


DEFAULT_EMBEDDER = "fastembed:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def make_embedder():
    """TEAMPULSE_EMBEDDER: "fastembed:<model>" (default, ~0.2 GB, no PyTorch), "hash" (offline keyword
    stand-in), or a sentence-transformers model name such as intfloat/multilingual-e5-small (needs
    `pip install sentence-transformers`, ~2 GB with PyTorch)."""
    choice = config.get("TEAMPULSE_EMBEDDER", DEFAULT_EMBEDDER)
    if choice == "hash":
        return HashEmbedder()
    if choice.startswith("fastembed:"):
        return FastEmbedEmbedder(choice.split(":", 1)[1])
    return SentenceTransformerEmbedder(choice)


# ---- text preparation ----------------------------------------------------------------------

def event_text(row: sqlite3.Row, parent_title: str | None = None) -> str:
    """What gets embedded: a short header (so 'who/where/status' is searchable) plus the content."""
    header = [f"[{row['source']} {row['kind']}]"]
    if row["channel"]:
        header.append(f"in {row['channel']}")
    if row["author_name"]:
        header.append(f"by {row['author_name']}")
    if row["status"]:
        header.append(f"status: {row['status']}")
    assignees = json.loads(row["assignees"] or "[]")
    if assignees:
        header.append("assigned: " + ", ".join(assignees))
    lines = [" ".join(header)]
    if parent_title:
        lines.append(f"re: {parent_title}")
    if row["title"]:
        lines.append(row["title"])
    if row["body"]:
        lines.append(row["body"])
    return "\n".join(lines)


def chunk(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    pieces, start = [], 0
    while start < len(text):
        pieces.append(text[start:start + CHUNK_CHARS])
        start += CHUNK_CHARS - CHUNK_OVERLAP
    return pieces


def parent_title(conn, row) -> str | None:
    if not row["parent_id"]:
        return None
    parent = conn.execute("SELECT title, body FROM events WHERE id = ?", (row["parent_id"],)).fetchone()
    if parent is None:
        return None
    return parent["title"] or (parent["body"] or "")[:120]


def ts(iso: str | None) -> int:
    return int(datetime.fromisoformat(iso).timestamp()) if iso else 0


# ---- index ---------------------------------------------------------------------------------

class EventIndex:
    def __init__(self, conn: sqlite3.Connection, embedder=None, path: str | None = None):
        import chromadb

        self.conn = conn
        self.embedder = embedder or make_embedder()
        db_file = conn.execute("PRAGMA database_list").fetchone()[2] or ""
        default = str(Path(db_file).with_suffix("")) + "_chroma" if db_file else None
        path = path or config.get("TEAMPULSE_CHROMA", default)
        client = chromadb.PersistentClient(path=path) if path else chromadb.EphemeralClient()
        # One collection per embedding model, so switching models never mixes vectors.
        name = "events_" + re.sub(r"[^a-zA-Z0-9]+", "_", self.embedder.name)[-40:].strip("_")
        self.collection = client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})
        if self.collection.count() == 0:   # fresh collection: forget any old bookkeeping
            with conn:
                conn.execute("DELETE FROM indexed")

    def sync(self) -> dict:
        """Embed new or changed events and remove deleted ones. Safe to run any time."""
        rows = self.conn.execute(
            "SELECT e.* FROM events e LEFT JOIN indexed i ON i.event_id = e.id "
            "WHERE i.event_id IS NULL OR i.updated_at IS NOT e.updated_at").fetchall()
        gone = [r[0] for r in self.conn.execute(
            "SELECT event_id FROM indexed WHERE event_id NOT IN (SELECT id FROM events)")]

        for event_id in gone + [r["id"] for r in rows]:
            self.collection.delete(where={"event_id": event_id})

        ids, texts, metas, book = [], [], [], []
        for row in rows:
            pieces = chunk(event_text(row, parent_title(self.conn, row)))
            for i, piece in enumerate(pieces):
                ids.append(f"{row['id']}::{i}")
                texts.append(piece)
                metas.append({
                    "event_id": row["id"], "source": row["source"], "kind": row["kind"],
                    "author": row["author_name"] or "", "status": row["status"] or "",
                    "updated_ts": ts(row["updated_at"]),
                })
            book.append((row["id"], row["updated_at"], len(pieces)))

        for start in range(0, len(ids), 64):
            batch = slice(start, start + 64)
            self.collection.add(ids=ids[batch], documents=texts[batch], metadatas=metas[batch],
                                embeddings=self.embedder.documents(texts[batch]))
        with self.conn:
            self.conn.executemany("DELETE FROM indexed WHERE event_id = ?", [(g,) for g in gone])
            self.conn.executemany(
                "INSERT OR REPLACE INTO indexed (event_id, updated_at, chunks) VALUES (?, ?, ?)", book)
        return {"embedded": len(rows), "chunks": len(ids), "removed": len(gone)}

    def search(self, query: str, k: int = 8, sources: list[str] | None = None,
               since_ts: int | None = None, kinds: list[str] | None = None) -> list[dict]:
        filters = []
        if sources:
            filters.append({"source": {"$in": sources}})
        if kinds:
            filters.append({"kind": {"$in": kinds}})
        if since_ts:
            filters.append({"updated_ts": {"$gte": since_ts}})
        where = filters[0] if len(filters) == 1 else ({"$and": filters} if filters else None)
        total = self.collection.count()
        if total == 0:
            return []
        result = self.collection.query(query_embeddings=[self.embedder.query(query)],
                                       n_results=min(k, total), where=where)
        hits = []
        for meta, doc, dist in zip(result["metadatas"][0], result["documents"][0], result["distances"][0]):
            hits.append({"event_id": meta["event_id"], "score": round(1 - dist, 4), "text": doc})
        return hits


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = db.connect()
    stats = EventIndex(conn).sync()
    print(f"Index updated: {stats}")


if __name__ == "__main__":
    main()
