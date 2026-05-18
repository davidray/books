"""SQLite-backed data store for Dabble exports.

Use ``DabbleDatabase.import_from_export`` to convert a ``DabbleExport`` (JSON)
into a SQLite database once.  Subsequent reads go directly to SQLite for fast
queries and FTS5 full-text search — no JSON parsing or patch replay at query
time.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .export_loader import (
    CONTAINER_TYPES,
    TEXT_FIELD_NAMES,
    TITLE_FIELD_NAMES,
    DabbleExport,
)

_SCHEMA = """
CREATE TABLE projects (
    id        TEXT PRIMARY KEY,
    title     TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE docs (
    id               TEXT NOT NULL,
    project_id       TEXT NOT NULL REFERENCES projects(id),
    type             TEXT,
    title            TEXT,
    doc_json         TEXT NOT NULL DEFAULT '{}',
    is_in_manuscript INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (id, project_id)
);

CREATE INDEX docs_project_idx ON docs (project_id);
CREATE INDEX docs_manuscript_idx ON docs (project_id, is_in_manuscript);

CREATE TABLE doc_children (
    parent_id  TEXT NOT NULL,
    project_id TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    position   INTEGER NOT NULL,
    PRIMARY KEY (parent_id, project_id, position)
);

CREATE INDEX doc_children_parent_idx ON doc_children (parent_id, project_id);

CREATE TABLE doc_text (
    doc_id     TEXT NOT NULL,
    project_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    content    TEXT NOT NULL,
    PRIMARY KEY (doc_id, project_id, field_name)
);

CREATE VIRTUAL TABLE doc_text_fts USING fts5 (
    doc_id     UNINDEXED,
    project_id UNINDEXED,
    field_name UNINDEXED,
    content
);
"""


def _drop_tables(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS doc_text_fts")
    conn.execute("DROP TABLE IF EXISTS doc_text")
    conn.execute("DROP TABLE IF EXISTS doc_children")
    conn.execute("DROP INDEX IF EXISTS docs_manuscript_idx")
    conn.execute("DROP INDEX IF EXISTS docs_project_idx")
    conn.execute("DROP TABLE IF EXISTS docs")
    conn.execute("DROP TABLE IF EXISTS projects")


def _doc_title(doc_id: str, doc: dict[str, Any]) -> str:
    for field_name in TITLE_FIELD_NAMES:
        value = doc.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return doc_id


def _extract_text_fields(doc: dict[str, Any]) -> dict[str, str]:
    from .patches import text_value_to_plain_text

    extracted: dict[str, str] = {}
    for field_name in TEXT_FIELD_NAMES:
        value = text_value_to_plain_text(doc.get(field_name))
        if value.strip():
            extracted[field_name] = value
    return extracted


class DabbleDatabase:
    """Read-only interface to a SQLite database built from a Dabble export."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    @classmethod
    def import_from_export(
        cls,
        export: DabbleExport,
        db_path: str | Path,
    ) -> "DabbleDatabase":
        """Convert a ``DabbleExport`` to SQLite.

        All patches are applied before writing so queries never need to replay
        change history.  Safe to re-run — the database is rebuilt from scratch.
        """
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(db_path)
        try:
            _drop_tables(conn)
            conn.executescript(_SCHEMA)

            projects = export.list_projects()
            for project_info in projects:
                project_id = project_info["project_id"]
                meta = export.project_meta_by_id.get(project_id, {})
                conn.execute(
                    "INSERT INTO projects (id, title, meta_json) VALUES (?, ?, ?)",
                    (project_id, project_info["title"], json.dumps(meta, ensure_ascii=False)),
                )

                try:
                    record = export.get_project(project_id)
                except KeyError:
                    continue

                docs = record.state.get("docs", {})
                manuscript_ids = export._manuscript_doc_ids(docs)

                for doc_id, doc in docs.items():
                    title = _doc_title(doc_id, doc)
                    is_manuscript = 1 if doc_id in manuscript_ids else 0
                    conn.execute(
                        "INSERT INTO docs (id, project_id, type, title, doc_json, is_in_manuscript)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            doc_id,
                            project_id,
                            doc.get("type"),
                            title,
                            json.dumps(doc, ensure_ascii=False),
                            is_manuscript,
                        ),
                    )

                    for position, child_id in enumerate(doc.get("children", [])):
                        conn.execute(
                            "INSERT INTO doc_children (parent_id, project_id, child_id, position)"
                            " VALUES (?, ?, ?, ?)",
                            (doc_id, project_id, child_id, position),
                        )

                    for field_name, content in _extract_text_fields(doc).items():
                        conn.execute(
                            "INSERT INTO doc_text (doc_id, project_id, field_name, content)"
                            " VALUES (?, ?, ?, ?)",
                            (doc_id, project_id, field_name, content),
                        )
                        conn.execute(
                            "INSERT INTO doc_text_fts (doc_id, project_id, field_name, content)"
                            " VALUES (?, ?, ?, ?)",
                            (doc_id, project_id, field_name, content),
                        )

            conn.commit()
        finally:
            conn.close()

        return cls(db_path)

    # ------------------------------------------------------------------
    # Public query interface  (mirrors DabbleExport)
    # ------------------------------------------------------------------

    def list_projects(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, title FROM projects ORDER BY lower(title)"
        ).fetchall()
        return [{"project_id": row["id"], "title": row["title"]} for row in rows]

    def resolve_project_id(self, project_ref: str) -> str:
        """Resolve a project ID or title to a canonical project ID."""
        conn = self._get_conn()
        row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_ref,)).fetchone()
        if row:
            return row["id"]
        rows = conn.execute(
            "SELECT id FROM projects WHERE lower(title) = lower(?)", (project_ref,)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
        if len(rows) > 1:
            raise ValueError(f"Project title '{project_ref}' is ambiguous; pass a project ID instead.")
        raise ValueError(f"Project not found: {project_ref}")

    def build_outline(self, project_id: str) -> dict[str, Any]:
        conn = self._get_conn()
        project_row = conn.execute(
            "SELECT title FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project_row:
            raise KeyError(f"Project not found: {project_id}")

        docs = self._load_docs(project_id)
        manuscript_ids = self._manuscript_doc_ids(docs)
        roots = self._manuscript_outline_roots(docs, manuscript_ids)

        return {
            "project_id": project_id,
            "title": project_row["title"],
            "books": [self._outline_node(docs, doc_id, manuscript_ids) for doc_id in roots if doc_id in docs],
        }

    def chapter_packets(self, project_id: str) -> list[dict[str, Any]]:
        docs = self._load_docs(project_id)
        manuscript_ids = self._manuscript_doc_ids(docs)
        packets: list[dict[str, Any]] = []
        seen: set[str] = set()
        for root_id in self._manuscript_outline_roots(docs, manuscript_ids):
            for chapter_id in self._chapters_in_outline_order(docs, root_id, manuscript_ids):
                if chapter_id in seen:
                    continue
                seen.add(chapter_id)
                packets.append(self.chapter_packet(project_id, chapter_id))
        return packets

    def chapter_packet(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        conn = self._get_conn()
        project_row = conn.execute(
            "SELECT title FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project_row:
            raise KeyError(f"Project not found: {project_id}")
        project_title = project_row["title"]

        docs = self._load_docs(project_id)
        manuscript_ids = self._manuscript_doc_ids(docs)
        if chapter_id not in docs:
            raise KeyError(f"Unknown chapter id: {chapter_id}")
        if chapter_id not in manuscript_ids:
            raise KeyError(f"Chapter {chapter_id} is not under manuscript")

        chapter = docs[chapter_id]
        descendent_ids = [
            doc_id
            for doc_id in [chapter_id, *self._descendants_in_order(docs, chapter_id)]
            if doc_id in manuscript_ids
        ]

        source_docs: list[dict[str, Any]] = []
        combined_text_parts: list[str] = []
        included_ids: set[str] = set()

        for doc_id in descendent_ids:
            doc = docs[doc_id]
            extracted = self._load_text_fields(project_id, doc_id)
            if not extracted:
                continue
            included_ids.add(doc_id)
            source_docs.append(
                {
                    "id": doc_id,
                    "type": doc.get("type"),
                    "title": _doc_title(doc_id, doc),
                    "fields": extracted,
                }
            )
            for field_name, value in extracted.items():
                combined_text_parts.append(f"[{doc_id}:{field_name}]\n{value.strip()}")

        if not source_docs:
            for doc_id in self._supplemental_chapter_doc_ids(project_id, docs, chapter_id, manuscript_ids):
                if doc_id in included_ids:
                    continue
                doc = docs[doc_id]
                extracted = self._load_text_fields(project_id, doc_id)
                if not extracted:
                    continue
                source_docs.append(
                    {
                        "id": doc_id,
                        "type": doc.get("type"),
                        "title": _doc_title(doc_id, doc),
                        "fields": extracted,
                    }
                )
                for field_name, value in extracted.items():
                    combined_text_parts.append(f"[{doc_id}:{field_name}]\n{value.strip()}")

        chapter_title = _doc_title(chapter_id, chapter)
        return {
            "project_id": project_id,
            "project_title": project_title,
            "chapter_id": chapter_id,
            "chapter_title": chapter_title,
            "chapter_type": chapter.get("type"),
            "source_doc_count": len(source_docs),
            "source_docs": source_docs,
            "combined_source_text": "\n\n".join(part for part in combined_text_parts if part.strip()),
            "prompt_template": self._summary_prompt(project_title, chapter_title),
        }

    def search_text(self, project_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        needle = query.strip()
        if not needle:
            return []

        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT f.doc_id, f.field_name, f.content,
                   d.type AS doc_type, d.title AS doc_title
              FROM doc_text_fts f
              JOIN docs d ON d.id = f.doc_id AND d.project_id = f.project_id
             WHERE f.project_id = ?
               AND doc_text_fts MATCH ?
             LIMIT ?
            """,
            (project_id, needle, limit),
        ).fetchall()

        matches: list[dict[str, Any]] = []
        for row in rows:
            content = row["content"]
            lower_content = content.lower()
            lower_needle = needle.lower()
            index = lower_content.find(lower_needle)
            if index == -1:
                index = 0
            start = max(0, index - 100)
            end = min(len(content), index + len(needle) + 100)
            matches.append(
                {
                    "doc_id": row["doc_id"],
                    "doc_type": row["doc_type"],
                    "title": row["doc_title"],
                    "field": row["field_name"],
                    "snippet": content[start:end].replace("\n", " ").strip(),
                }
            )
        return matches

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_docs(self, project_id: str) -> dict[str, dict[str, Any]]:
        """Load all docs for a project as a flat dict (same shape as DabbleExport.state["docs"])."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, doc_json FROM docs WHERE project_id = ?", (project_id,)
        ).fetchall()
        return {row["id"]: json.loads(row["doc_json"]) for row in rows}

    def _load_text_fields(self, project_id: str, doc_id: str) -> dict[str, str]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT field_name, content FROM doc_text WHERE doc_id = ? AND project_id = ?",
            (doc_id, project_id),
        ).fetchall()
        return {row["field_name"]: row["content"] for row in rows}

    def _manuscript_doc_ids(self, docs: dict[str, dict[str, Any]]) -> set[str]:
        manuscript_node = docs.get("manuscript")
        if manuscript_node and manuscript_node.get("type") == "novel_manuscript":
            ids = {"manuscript"}
            ids.update(self._descendants_in_order(docs, "manuscript"))
            return ids
        return {
            doc_id
            for doc_id, doc in docs.items()
            if str(doc.get("type", "")).startswith("novel_") and doc.get("type") != "novel_notes"
        }

    def _manuscript_outline_roots(self, docs: dict[str, dict[str, Any]], manuscript_ids: set[str]) -> list[str]:
        manuscript_node = docs.get("manuscript")
        if manuscript_node and manuscript_node.get("type") == "novel_manuscript":
            return [
                doc_id
                for doc_id in manuscript_node.get("children", [])
                if doc_id in docs and doc_id in manuscript_ids
            ]
        return [
            doc_id
            for doc_id, doc in docs.items()
            if doc.get("type") == "novel_book" and doc_id in manuscript_ids
        ]

    def _outline_node(self, docs: dict[str, dict[str, Any]], doc_id: str, allowed_ids: set[str]) -> dict[str, Any]:
        doc = docs[doc_id]
        return {
            "id": doc_id,
            "type": doc.get("type"),
            "title": _doc_title(doc_id, doc),
            "children": [
                self._outline_node(docs, child_id, allowed_ids)
                for child_id in doc.get("children", [])
                if child_id in docs and child_id in allowed_ids
            ],
        }

    def _descendants_in_order(self, docs: dict[str, dict[str, Any]], doc_id: str) -> list[str]:
        ordered: list[str] = []
        for child_id in docs.get(doc_id, {}).get("children", []):
            if child_id not in docs:
                continue
            ordered.append(child_id)
            ordered.extend(self._descendants_in_order(docs, child_id))
        return ordered

    def _chapters_in_outline_order(
        self,
        docs: dict[str, dict[str, Any]],
        doc_id: str,
        allowed_ids: set[str],
    ) -> list[str]:
        ordered: list[str] = []
        node = docs.get(doc_id)
        if not node:
            return ordered
        if node.get("type") == "novel_chapter" and doc_id in allowed_ids:
            ordered.append(doc_id)
        for child_id in node.get("children", []):
            if child_id not in docs or child_id not in allowed_ids:
                continue
            ordered.extend(self._chapters_in_outline_order(docs, child_id, allowed_ids))
        return ordered

    def _supplemental_chapter_doc_ids(
        self,
        project_id: str,
        docs: dict[str, dict[str, Any]],
        chapter_id: str,
        allowed_ids: set[str],
    ) -> list[str]:
        chapter = docs[chapter_id]
        chapter_title = _doc_title(chapter_id, chapter)
        normalized_chapter_title = self._normalize_title(chapter_title)
        candidates: list[tuple[int, str]] = []

        for doc_id, doc in docs.items():
            if doc_id not in allowed_ids:
                continue
            if doc_id == chapter_id or doc.get("type") in CONTAINER_TYPES:
                continue

            score = 0
            if doc.get("parentId") == chapter_id or doc.get("oldParentId") == chapter_id:
                score += 100

            doc_title = _doc_title(doc_id, doc)
            normalized_doc_title = self._normalize_title(doc_title)
            if normalized_doc_title and normalized_doc_title == normalized_chapter_title:
                score += 90
            elif normalized_doc_title and normalized_chapter_title and normalized_doc_title.startswith(normalized_chapter_title):
                score += 70
            elif normalized_doc_title and normalized_chapter_title and normalized_chapter_title in normalized_doc_title:
                score += 60

            if score >= 60 and self._load_text_fields(project_id, doc_id):
                candidates.append((score, doc_id))

        candidates.sort(key=lambda item: (-item[0], _doc_title(item[1], docs[item[1]]).lower(), item[1]))
        return [doc_id for _, doc_id in candidates[:5]]

    @staticmethod
    def _normalize_title(value: str) -> str:
        return " ".join(value.lower().replace(":", " ").replace("-", " ").split())

    @staticmethod
    def _summary_prompt(project_title: str, chapter_title: str) -> str:
        return (
            "Write a brief chapter summary using only the supplied source text. "
            "Do not invent events, motivations, or names that are not explicitly present. "
            "If the source is incomplete, say so directly. "
            f"Project: {project_title}. Chapter: {chapter_title}. "
            "Return 3-5 sentences followed by a short bullet list of named characters or entities explicitly mentioned."
        )
