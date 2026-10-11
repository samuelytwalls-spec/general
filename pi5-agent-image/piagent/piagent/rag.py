"""Retrieval over /srv/docs with a local embedding model and sqlite-vec.

Documents are split into overlapping chunks, embedded by the llama.cpp
embedding server (bge-small-en-v1.5 by default) and stored in a vec0 virtual
table. Indexing is incremental: unchanged files (same size and mtime, or same
content hash) are skipped and deleted files are dropped.
"""

from __future__ import annotations

import fcntl
import hashlib
import html.parser
import os
import re
import shutil
import sqlite3
import subprocess
import time

from .config import RAGConfig
from .llm import LlamaClient

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT NOT NULL,
    indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
"""


class _HTMLText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "pre", "section"):
            self.parts.append("\n\n" if tag != "br" else "\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def extract_text(path: str) -> str | None:
    """Return the text of a document, or None if it cannot be read as text."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        if not shutil.which("pdftotext"):
            return None  # install poppler-utils to index PDFs
        proc = subprocess.run(
            ["pdftotext", "-layout", "-q", path, "-"],
            capture_output=True, timeout=120, check=False,
        )
        return proc.stdout.decode(errors="replace") if proc.returncode == 0 else None
    with open(path, "rb") as fh:
        raw = fh.read()
    if b"\x00" in raw[:4096]:
        return None
    text = raw.decode("utf-8", errors="replace")
    if ext in (".html", ".htm"):
        parser = _HTMLText()
        parser.feed(text)
        text = "".join(parser.parts)
    return text


def _pieces(paragraph: str, size: int) -> list[str]:
    if len(paragraph) <= size:
        return [paragraph]
    out = []
    start = 0
    while start < len(paragraph):
        end = min(start + size, len(paragraph))
        if end < len(paragraph):
            cut = paragraph.rfind(" ", start + size // 2, end)
            if cut > start:
                end = cut
        piece = paragraph[start:end].strip()
        if piece:
            out.append(piece)
        start = end
    return out


def chunk_text(text: str, size: int = 1200, overlap: int = 200) -> list[str]:
    """Pack paragraphs into chunks of at most ``size`` characters.

    Consecutive chunks share up to ``overlap`` trailing characters so that a
    passage cut at a boundary can still be found.
    """
    overlap = max(0, min(overlap, size // 2))
    text = re.sub(r"[ \t]+\n", "\n", text.replace("\r\n", "\n"))
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for para in paragraphs:
        for piece in _pieces(para, max(1, size - overlap - 2)):  # 2 = the "\n\n" joiner
            candidate = f"{cur}\n\n{piece}" if cur else piece
            if len(candidate) <= size:
                cur = candidate
                continue
            chunks.append(cur)
            tail = cur[-overlap:] if overlap else ""
            if tail and " " in tail:
                tail = tail[tail.index(" ") + 1:]
            cur = f"{tail}\n\n{piece}" if tail else piece
    if cur:
        chunks.append(cur)
    return chunks


class DocIndex:
    def __init__(self, cfg: RAGConfig, llm: LlamaClient):
        self.cfg = cfg
        self.llm = llm

    # -- database -------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        import sqlite_vec  # imported lazily so the rest of piagent works without it

        os.makedirs(os.path.dirname(self.cfg.db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.cfg.db_path, timeout=30, check_same_thread=False)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        return conn

    @staticmethod
    def _meta(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _ensure_vec_table(self, conn: sqlite3.Connection, dim: int) -> None:
        model = self.llm.cfg.embed_model
        if self._meta(conn, "dim") == str(dim) and self._meta(conn, "model") == model:
            return
        # New database, or the embedding model changed: rebuild from scratch.
        conn.execute("DROP TABLE IF EXISTS chunk_vec")
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM documents")
        conn.execute(
            f"CREATE VIRTUAL TABLE chunk_vec USING vec0(embedding float[{int(dim)}] distance_metric=cosine)"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            [("dim", str(dim)), ("model", model)],
        )
        conn.commit()

    # -- indexing -------------------------------------------------------
    def _iter_files(self):
        exts = {e.lower() for e in self.cfg.extensions}
        for root, dirs, files in os.walk(self.cfg.docs_dir):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                if name.startswith("."):
                    continue
                path = os.path.join(root, name)
                if os.path.splitext(name)[1].lower() not in exts or not os.path.isfile(path):
                    continue
                yield path

    def _delete_doc(self, conn: sqlite3.Connection, doc_id: int) -> None:
        ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,))]
        conn.executemany("DELETE FROM chunk_vec WHERE rowid = ?", [(i,) for i in ids])
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

    def index(self, full: bool = False, log=lambda msg: None) -> dict:
        """(Re)index the docs directory. Returns counts of what changed."""
        import sqlite_vec

        os.makedirs(os.path.dirname(self.cfg.db_path) or ".", exist_ok=True)
        lock = open(self.cfg.db_path + ".lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return {"status": "busy", "message": "another indexing run is in progress"}
        stats = {"status": "ok", "added": 0, "updated": 0, "unchanged": 0, "removed": 0,
                 "skipped": 0, "chunks": 0, "seconds": 0.0}
        started = time.monotonic()
        conn = self.connect()
        try:
            dim = len(self.llm.embed(["dimension probe"])[0])
            if full:
                conn.execute("DELETE FROM meta WHERE key = 'dim'")
            self._ensure_vec_table(conn, dim)
            known = {row[1]: row for row in conn.execute("SELECT id, path, size, mtime, sha256 FROM documents")}
            seen = set()
            for path in self._iter_files():
                rel = os.path.relpath(path, self.cfg.docs_dir)
                seen.add(rel)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if st.st_size > self.cfg.max_file_bytes:
                    stats["skipped"] += 1
                    continue
                old = known.get(rel)
                if old and old[2] == st.st_size and old[3] == st.st_mtime:
                    stats["unchanged"] += 1
                    continue
                with open(path, "rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
                if old and old[4] == digest:
                    conn.execute("UPDATE documents SET size = ?, mtime = ? WHERE id = ?", (st.st_size, st.st_mtime, old[0]))
                    stats["unchanged"] += 1
                    continue
                text = extract_text(path)
                if not text or not text.strip():
                    stats["skipped"] += 1
                    if old:
                        self._delete_doc(conn, old[0])
                    continue
                chunks = chunk_text(text, self.cfg.chunk_chars, self.cfg.chunk_overlap)
                vectors: list[list[float]] = []
                for i in range(0, len(chunks), self.cfg.batch_size):
                    batch = chunks[i:i + self.cfg.batch_size]
                    vectors.extend(self.llm.embed([f"{rel}\n{c}" for c in batch]))
                if old:
                    self._delete_doc(conn, old[0])
                cur = conn.execute(
                    "INSERT INTO documents(path, size, mtime, sha256, indexed_at) VALUES (?, ?, ?, ?, ?)",
                    (rel, st.st_size, st.st_mtime, digest, time.time()),
                )
                doc_id = cur.lastrowid
                for ord_, (chunk, vec) in enumerate(zip(chunks, vectors)):
                    cid = conn.execute(
                        "INSERT INTO chunks(doc_id, ord, text) VALUES (?, ?, ?)", (doc_id, ord_, chunk)
                    ).lastrowid
                    conn.execute(
                        "INSERT INTO chunk_vec(rowid, embedding) VALUES (?, ?)",
                        (cid, sqlite_vec.serialize_float32(vec)),
                    )
                conn.commit()
                stats["updated" if old else "added"] += 1
                stats["chunks"] += len(chunks)
                log(f"indexed {rel} ({len(chunks)} chunks)")
            for rel, row in known.items():
                if rel not in seen:
                    self._delete_doc(conn, row[0])
                    stats["removed"] += 1
                    log(f"removed {rel}")
            conn.commit()
        finally:
            conn.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        stats["seconds"] = round(time.monotonic() - started, 2)
        return stats

    # -- querying -------------------------------------------------------
    def search(self, query: str, k: int | None = None) -> list[dict]:
        import sqlite_vec

        k = max(1, min(int(k or self.cfg.top_k), 20))
        conn = self.connect()
        try:
            if self._meta(conn, "dim") is None:
                return []
            vec = self.llm.embed([self.cfg.query_prefix + query])[0]
            hits = conn.execute(
                "SELECT rowid, distance FROM chunk_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (sqlite_vec.serialize_float32(vec), k),
            ).fetchall()
            results = []
            for rowid, distance in hits:
                row = conn.execute(
                    "SELECT d.path, c.ord, c.text FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id = ?",
                    (rowid,),
                ).fetchone()
                if row:
                    results.append({"path": row[0], "chunk": row[1], "score": round(1.0 - float(distance), 4), "text": row[2]})
            return results
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = self.connect()
        try:
            docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            return {"documents": docs, "chunks": chunks, "model": self._meta(conn, "model"),
                    "dim": self._meta(conn, "dim"), "docs_dir": self.cfg.docs_dir}
        finally:
            conn.close()
