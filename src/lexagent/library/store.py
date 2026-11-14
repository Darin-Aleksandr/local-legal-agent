"""Библиотека норм и практики: SQLite на диске, версии документов, поиск по словам и по смыслу.

Каждый документ хранится с источником (URL), датой, редакцией и хэшем. Новая редакция того же
документа не перезаписывает старую: старая получает статус «заменён» и из поиска исчезает, но
остаётся в архиве — всегда видно, что и когда поменялось.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from lexagent.library.authority import Kind

Embed = Callable[[list[str]], list[list[float]]]

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,              -- одно и то же для всех редакций документа: law:40-ФЗ, vs:41-КГ25-32-К4
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    number TEXT DEFAULT '',
    issued TEXT,                    -- дата документа (ISO)
    edition TEXT DEFAULT '',        -- «ред. от 31.07.2025» для законов
    url TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    sha256 TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- active | superseded | repealed
    superseded_by INTEGER,
    note TEXT DEFAULT '',
    UNIQUE (key, sha256)
);
CREATE INDEX IF NOT EXISTS documents_key ON documents(key, status);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id),
    ord INTEGER NOT NULL,
    heading TEXT DEFAULT '',
    text TEXT NOT NULL,
    embedding BLOB
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, heading, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY,
    new_chunk INTEGER NOT NULL,
    old_chunk INTEGER NOT NULL,
    loser_chunk INTEGER NOT NULL,   -- этот фрагмент больше не участвует в поиске: его позиция преодолена
    topic TEXT NOT NULL,
    summary TEXT NOT NULL,
    reason TEXT NOT NULL,
    created TEXT NOT NULL,
    UNIQUE (new_chunk, old_chunk)
);
"""


@dataclass(frozen=True)
class DocMeta:
    key: str
    title: str
    kind: Kind
    number: str = ""
    issued: date | None = None
    edition: str = ""
    url: str = ""
    tags: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Document:
    id: int
    key: str
    title: str
    kind: Kind
    number: str
    issued: date | None
    edition: str
    url: str
    tags: tuple[str, ...]
    status: str
    fetched_at: str

    def cite(self) -> str:
        """Как сослаться на документ в тексте: название, номер, дата."""
        parts = [self.title]
        if self.number and self.number not in self.title:
            parts.append(f"№ {self.number}")
        if self.issued and self.issued.strftime("%d.%m.%Y") not in self.title:
            parts.append(f"от {self.issued:%d.%m.%Y}")
        if self.edition:
            parts.append(f"({self.edition})")
        return " ".join(parts)


@dataclass(frozen=True)
class Hit:
    document: Document
    chunk_id: int
    heading: str
    text: str
    score: float


@dataclass
class AddResult:
    doc_id: int
    status: str  # new | updated | same
    replaced: list[int] = field(default_factory=list)


def text_hash(text: str) -> str:
    """Хэш по смыслу, а не по байтам: пробелы и переносы не создают «новую редакцию»."""
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode()).hexdigest()


class Library:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)  # окно работает в потоках; доступ под замком
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._vectors: tuple[np.ndarray, list[int]] | None = None

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- write
    def add(self, meta: DocMeta, text: str, embed: Embed | None = None) -> AddResult:
        digest = text_hash(text)
        same = self.db.execute("SELECT id FROM documents WHERE key=? AND sha256=?", (meta.key, digest)).fetchone()
        if same:
            return AddResult(same["id"], "same")
        previous = [
            r["id"] for r in self.db.execute("SELECT id FROM documents WHERE key=? AND status='active'", (meta.key,))
        ]
        cursor = self.db.execute(
            "INSERT INTO documents (key, title, kind, number, issued, edition, url, tags, sha256, fetched_at, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                meta.key,
                meta.title,
                meta.kind.value,
                meta.number,
                meta.issued.isoformat() if meta.issued else None,
                meta.edition,
                meta.url,
                ",".join(meta.tags),
                digest,
                _now(),
                meta.note,
            ),
        )
        doc_id = int(cursor.lastrowid or 0)
        self._insert_chunks(doc_id, chunk(text, meta.kind), embed)
        for old in previous:  # новая редакция вытесняет старую из поиска, но не из архива
            self.db.execute("UPDATE documents SET status='superseded', superseded_by=? WHERE id=?", (doc_id, old))
        self.db.commit()
        self._vectors = None
        return AddResult(doc_id, "updated" if previous else "new", previous)

    def _insert_chunks(self, doc_id: int, parts: list[tuple[str, str]], embed: Embed | None) -> None:
        vectors: list[list[float] | None] = [None] * len(parts)
        if embed and parts:
            for start in range(0, len(parts), 32):
                batch = parts[start : start + 32]
                for offset, vector in enumerate(embed([f"{h}\n{t}" for h, t in batch])):
                    vectors[start + offset] = vector
        for ord_, ((heading, text), vector) in enumerate(zip(parts, vectors, strict=True)):
            blob = np.asarray(vector, dtype=np.float32).tobytes() if vector else None
            cursor = self.db.execute(
                "INSERT INTO chunks (doc_id, ord, heading, text, embedding) VALUES (?,?,?,?,?)",
                (doc_id, ord_, heading, text, blob),
            )
            self.db.execute(
                "INSERT INTO chunks_fts (rowid, text, heading) VALUES (?,?,?)", (cursor.lastrowid, text, heading)
            )

    def set_status(self, doc_id: int, status: str, note: str = "") -> None:
        self.db.execute("UPDATE documents SET status=?, note=? WHERE id=?", (status, note, doc_id))
        self.db.commit()
        self._vectors = None

    def add_conflict(
        self, new_chunk: int, old_chunk: int, loser_chunk: int, topic: str, summary: str, reason: str
    ) -> bool:
        if self.has_conflict(new_chunk, old_chunk):
            return False
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO conflicts (new_chunk, old_chunk, loser_chunk, topic, summary, reason, created)"
            " VALUES (?,?,?,?,?,?,?)",
            (new_chunk, old_chunk, loser_chunk, topic, summary, reason, _now()),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def has_conflict(self, a: int, b: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM conflicts WHERE (new_chunk=? AND old_chunk=?) OR (new_chunk=? AND old_chunk=?)", (a, b, b, a)
        ).fetchone()
        return row is not None

    def overruled(self) -> set[int]:
        return {r["loser_chunk"] for r in self.db.execute("SELECT loser_chunk FROM conflicts")}

    def chunk_doc(self, chunk_id: int) -> Document:
        row = self.db.execute("SELECT doc_id FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        return self.document(row["doc_id"])

    # ---------------------------------------------------------------- read
    def document(self, doc_id: int) -> Document:
        row = self.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return _document(row)

    def documents(self, status: str | None = "active") -> list[Document]:
        sql = "SELECT * FROM documents" + (" WHERE status=?" if status else "") + " ORDER BY issued DESC, id DESC"
        return [_document(r) for r in self.db.execute(sql, (status,) if status else ())]

    def known_urls(self) -> set[str]:
        return {r["url"] for r in self.db.execute("SELECT url FROM documents WHERE url != ''")}

    def active_by_key(self, key: str) -> Document | None:
        row = self.db.execute("SELECT * FROM documents WHERE key=? AND status='active'", (key,)).fetchone()
        return _document(row) if row else None

    def chunks_of(self, doc_id: int) -> list[tuple[int, str, str]]:
        return [
            (r["id"], r["heading"], r["text"])
            for r in self.db.execute("SELECT id, heading, text FROM chunks WHERE doc_id=? ORDER BY ord", (doc_id,))
        ]

    def conflicts(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM conflicts ORDER BY id DESC"))

    # ---------------------------------------------------------------- search
    def search(
        self,
        query: str,
        *,
        k: int = 8,
        embed: Embed | None = None,
        kinds: Iterable[Kind] | None = None,
        tags: Iterable[str] | None = None,
    ) -> list[Hit]:
        """Гибридный поиск: точные слова (номера статей, пунктов) + смысл. Только действующие документы."""
        allowed = self._allowed(kinds, tags)
        if not allowed:
            return []
        ranked: dict[int, float] = {}
        overruled = self.overruled()
        for rank, chunk_id in enumerate(self._fts(query, allowed)):
            ranked[chunk_id] = ranked.get(chunk_id, 0) + 1 / (60 + rank)
        if embed is not None:
            for rank, chunk_id in enumerate(self._semantic(query, embed, allowed)):
                ranked[chunk_id] = ranked.get(chunk_id, 0) + 1 / (60 + rank)
        best = sorted(
            ((c, s) for c, s in ranked.items() if c not in overruled), key=lambda item: item[1], reverse=True
        )[:k]
        hits = []
        for chunk_id, score in best:
            row = self.db.execute("SELECT doc_id, heading, text FROM chunks WHERE id=?", (chunk_id,)).fetchone()
            hits.append(Hit(self.document(row["doc_id"]), chunk_id, row["heading"], row["text"], score))
        return hits

    def _allowed(self, kinds: Iterable[Kind] | None, tags: Iterable[str] | None) -> set[int]:
        rows = self.db.execute("SELECT id, kind, tags FROM documents WHERE status='active'").fetchall()
        kinds_set = {k.value for k in kinds} if kinds else None
        tags_set = set(tags) if tags else None
        return {
            r["id"]
            for r in rows
            if (kinds_set is None or r["kind"] in kinds_set)
            and (tags_set is None or tags_set & set(filter(None, r["tags"].split(","))))
        }

    def _fts(self, query: str, allowed: set[int], limit: int = 40) -> list[int]:
        terms = fts_terms(query)
        if not terms:
            return []
        rows = self.db.execute(
            "SELECT chunks.id, chunks.doc_id FROM chunks_fts JOIN chunks ON chunks.id = chunks_fts.rowid"
            " WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
            (" OR ".join(terms), limit * 3),
        )
        return [r["id"] for r in rows if r["doc_id"] in allowed][:limit]

    def similar(
        self, chunk_id: int, embed: Embed, *, exclude_doc: int, limit: int = 3, threshold: float = 0.8
    ) -> list[tuple[int, float]]:
        """Фрагменты других действующих документов о том же — кандидаты на противоречие."""
        matrix, ids = self._matrix()
        row = self.db.execute("SELECT heading, text FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        if not ids or row is None:
            return []
        vector = np.asarray(embed([f"{row['heading']}\n{row['text']}"])[0], dtype=np.float32)
        scores = matrix @ (vector / (np.linalg.norm(vector) or 1))
        doc_of = dict(self.db.execute("SELECT id, doc_id FROM chunks").fetchall())
        found = []
        for i in np.argsort(-scores):
            if scores[i] < threshold or len(found) >= limit:
                break
            if doc_of.get(ids[i]) != exclude_doc:
                found.append((ids[i], float(scores[i])))
        return found

    def _semantic(self, query: str, embed: Embed, allowed: set[int], limit: int = 40) -> list[int]:
        matrix, ids = self._matrix()
        if not ids:
            return []
        vector = np.asarray(embed([query])[0], dtype=np.float32)
        scores = matrix @ (vector / (np.linalg.norm(vector) or 1))
        doc_of = dict(self.db.execute("SELECT id, doc_id FROM chunks WHERE embedding IS NOT NULL").fetchall())
        order = np.argsort(-scores)
        return [ids[i] for i in order if doc_of.get(ids[i]) in allowed][:limit]

    def _matrix(self) -> tuple[np.ndarray, list[int]]:
        if self._vectors is None:
            rows = self.db.execute(
                "SELECT chunks.id, embedding FROM chunks JOIN documents ON documents.id = chunks.doc_id"
                " WHERE embedding IS NOT NULL AND documents.status='active'"
            ).fetchall()
            if not rows:
                self._vectors = (np.zeros((0, 1), dtype=np.float32), [])
            else:
                matrix = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                matrix /= np.linalg.norm(matrix, axis=1, keepdims=True).clip(min=1e-9)
                self._vectors = (matrix, [r["id"] for r in rows])
        return self._vectors


# ---------------------------------------------------------------- chunking
LAW_HEADING = re.compile(r"^(Статья\s+\d+(?:\.\d+)*\.?.*|Глава\s+[\dIVXLC]+.*)$")
POINT = re.compile(r"^(\d{1,3})(?:\.(\d{1,2}))?\.\s")  # «56. При...» — пункт постановления Пленума или обзора


def chunk(text: str, kind: Kind = Kind.OTHER, size: int = 1400) -> list[tuple[str, str]]:
    """Куски по 1–1,5 тыс. символов с «адресом»: статья закона или пункт постановления — чтобы цитировать точно."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n(?=\s*(?:Статья\s|\d{1,3}\.\s))", text) if p.strip()]
    parts: list[tuple[str, str]] = []
    heading, buffer = "", ""

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            parts.append((heading, buffer.strip()))
        buffer = ""

    for paragraph in paragraphs:
        first_line = paragraph.splitlines()[0].strip()
        new_heading = None
        if kind is Kind.LAW and LAW_HEADING.match(first_line):
            new_heading = first_line[:120]
        elif kind in {Kind.PLENUM, Kind.REVIEW} and (m := POINT.match(first_line)):
            new_heading = f"п. {m.group(1)}" + (f".{m.group(2)}" if m.group(2) else "")
        if new_heading is not None:
            flush()
            heading = new_heading
        if len(buffer) + len(paragraph) > size:
            flush()
        buffer += paragraph + "\n\n"
        while len(buffer) > size * 1.5:  # очень длинный абзац режем по предложениям
            cut = buffer.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 2 else size
            parts.append((heading, buffer[:cut].strip()))
            buffer = buffer[cut:]
    flush()
    return parts


STOP = {
    "или",
    "для",
    "при",
    "что",
    "как",
    "это",
    "его",
    "она",
    "они",
    "был",
    "быть",
    "если",
    "также",
    "так",
    "все",
    "который",
    "которые",
    "после",
    "суда",
    "суд",
    "года",
    "статьи",
    "пункта",
}


def fts_terms(query: str) -> list[str]:
    """Слова запроса как префиксы: «неустойки» найдёт «неустойка», «неустойку» (русская морфология без словаря)."""
    terms = []
    for word in re.findall(r"[\wё]+", query.lower()):
        if word in STOP or len(word) < 3:
            if word.isdigit() or re.fullmatch(r"\d+[\-.]\w+", word):
                terms.append(f'"{word}"')
            continue
        stem = word[: max(4, len(word) - 2)] if len(word) > 5 and not word.isdigit() else word
        terms.append(f'"{stem}"*')
    return list(dict.fromkeys(terms))


def _document(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"],
        key=row["key"],
        title=row["title"],
        kind=Kind(row["kind"]),
        number=row["number"],
        issued=date.fromisoformat(row["issued"]) if row["issued"] else None,
        edition=row["edition"],
        url=row["url"],
        tags=tuple(filter(None, row["tags"].split(","))),
        status=row["status"],
        fetched_at=row["fetched_at"],
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
