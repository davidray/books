from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .patches import apply_patch, text_value_to_plain_text

TEXT_FIELD_NAMES = ("body", "text", "summary", "synopsis", "notes", "description")
TITLE_FIELD_NAMES = ("title", "name")
CONTAINER_TYPES = {"novel_book", "novel_chapter", "novel_folder", "novel_manuscript", "novel_notes"}


@dataclass(slots=True)
class ProjectRecord:
    project_id: str
    title: str
    snapshot: dict[str, Any]
    state: dict[str, Any]
    meta: dict[str, Any]


class DabbleExport:
    def __init__(self, export_path: Path, payload: dict[str, Any]):
        self.export_path = export_path
        self.payload = payload
        self.project_meta_by_id = {item["id"]: item for item in payload.get("project_metas", []) if item.get("id")}
        self.snapshots_by_project = self._group_by_project(payload.get("project_snapshots", []))
        self.changes_by_project = self._group_by_project(payload.get("project_changes", []))
        self._project_cache: dict[str, ProjectRecord] = {}

    @classmethod
    def from_file(cls, export_path: str | Path) -> "DabbleExport":
        export_path = Path(export_path)
        with export_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return cls(export_path=export_path, payload=payload)

    def _group_by_project(self, records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            project_id = record.get("projectId") or record.get("id")
            if not project_id:
                continue
            grouped.setdefault(project_id, []).append(record)
        for items in grouped.values():
            items.sort(key=lambda item: (item.get("committed") or 0, item.get("created") or 0, item.get("id") or ""))
        return grouped

    def list_projects(self) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        for project_id, meta in sorted(self.project_meta_by_id.items(), key=lambda item: (item[1].get("title") or item[0]).lower()):
            snapshot_count = len(self.snapshots_by_project.get(project_id, []))
            change_count = len(self.changes_by_project.get(project_id, []))
            projects.append(
                {
                    "project_id": project_id,
                    "title": meta.get("title") or meta.get("name") or project_id,
                    "snapshot_count": snapshot_count,
                    "change_count": change_count,
                }
            )
        return projects

    def get_project(self, project_id: str) -> ProjectRecord:
        if project_id in self._project_cache:
            return self._project_cache[project_id]

        snapshots = self.snapshots_by_project.get(project_id)
        if not snapshots:
            raise KeyError(f"Project {project_id} has no snapshots")

        latest_snapshot = max(snapshots, key=lambda item: (item.get("committed") or 0, item.get("created") or 0, item.get("version") or 0))
        state = deepcopy(latest_snapshot)
        trailing_changes = self._changes_after_snapshot(project_id, latest_snapshot)
        for change in trailing_changes:
            for operation in change.get("ops", []):
                apply_patch(state, operation)

        title = self.project_meta_by_id.get(project_id, {}).get("title") or self.project_meta_by_id.get(project_id, {}).get("name") or project_id
        record = ProjectRecord(
            project_id=project_id,
            title=title,
            snapshot=latest_snapshot,
            state=state,
            meta=self.project_meta_by_id.get(project_id, {}),
        )
        self._project_cache[project_id] = record
        return record

    def _changes_after_snapshot(self, project_id: str, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        changes = self.changes_by_project.get(project_id, [])
        if not changes:
            return []

        start_change_id = snapshot.get("changeId")
        if start_change_id:
            change_by_prev = {change.get("prevId"): change for change in changes if change.get("prevId")}
            ordered: list[dict[str, Any]] = []
            current = start_change_id
            seen: set[str] = set()
            while current in change_by_prev and current not in seen:
                change = change_by_prev[current]
                ordered.append(change)
                seen.add(current)
                current = change.get("id")
            if ordered:
                return ordered

        snapshot_cutoff = snapshot.get("committed") or snapshot.get("created") or 0
        snapshot_id = snapshot.get("changeId")
        return [
            change
            for change in changes
            if (change.get("committed") or change.get("created") or 0) > snapshot_cutoff and change.get("id") != snapshot_id
        ]

    def build_outline(self, project_id: str) -> dict[str, Any]:
        project = self.get_project(project_id)
        docs = project.state.get("docs", {})
        manuscript_ids = self._manuscript_doc_ids(docs)
        roots = self._manuscript_outline_roots(docs, manuscript_ids)

        return {
            "project_id": project.project_id,
            "title": project.title,
            "books": [self._outline_node(docs, doc_id, manuscript_ids) for doc_id in roots if doc_id in docs],
        }

    def _outline_node(self, docs: dict[str, dict[str, Any]], doc_id: str, allowed_ids: set[str]) -> dict[str, Any]:
        doc = docs[doc_id]
        return {
            "id": doc_id,
            "type": doc.get("type"),
            "title": self.doc_title(doc_id, doc),
            "children": [
                self._outline_node(docs, child_id, allowed_ids)
                for child_id in doc.get("children", [])
                if child_id in docs and child_id in allowed_ids
            ],
        }

    def doc_title(self, doc_id: str, doc: dict[str, Any]) -> str:
        for field_name in TITLE_FIELD_NAMES:
            value = doc.get(field_name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return doc_id

    def chapter_packets(self, project_id: str) -> list[dict[str, Any]]:
        project = self.get_project(project_id)
        docs = project.state.get("docs", {})
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

    def chapter_packet(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        project = self.get_project(project_id)
        docs = project.state.get("docs", {})
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
            extracted = self.extract_text_fields(doc)
            if not extracted:
                continue
            included_ids.add(doc_id)
            source_docs.append(
                {
                    "id": doc_id,
                    "type": doc.get("type"),
                    "title": self.doc_title(doc_id, doc),
                    "fields": extracted,
                }
            )
            for field_name, value in extracted.items():
                combined_text_parts.append(f"[{doc_id}:{field_name}]\n{value.strip()}")

        if not source_docs:
            for doc_id in self._supplemental_chapter_doc_ids(docs, chapter_id, manuscript_ids):
                if doc_id in included_ids:
                    continue
                doc = docs[doc_id]
                extracted = self.extract_text_fields(doc)
                if not extracted:
                    continue
                source_docs.append(
                    {
                        "id": doc_id,
                        "type": doc.get("type"),
                        "title": self.doc_title(doc_id, doc),
                        "fields": extracted,
                    }
                )
                for field_name, value in extracted.items():
                    combined_text_parts.append(f"[{doc_id}:{field_name}]\n{value.strip()}")

        return {
            "project_id": project.project_id,
            "project_title": project.title,
            "chapter_id": chapter_id,
            "chapter_title": self.doc_title(chapter_id, chapter),
            "chapter_type": chapter.get("type"),
            "source_doc_count": len(source_docs),
            "source_docs": source_docs,
            "combined_source_text": "\n\n".join(part for part in combined_text_parts if part.strip()),
            "prompt_template": self._summary_prompt(project.title, self.doc_title(chapter_id, chapter)),
        }

    def _supplemental_chapter_doc_ids(self, docs: dict[str, dict[str, Any]], chapter_id: str, allowed_ids: set[str]) -> list[str]:
        chapter = docs[chapter_id]
        chapter_title = self.doc_title(chapter_id, chapter)
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

            doc_title = self.doc_title(doc_id, doc)
            normalized_doc_title = self._normalize_title(doc_title)
            if normalized_doc_title and normalized_doc_title == normalized_chapter_title:
                score += 90
            elif normalized_doc_title and normalized_chapter_title and normalized_doc_title.startswith(normalized_chapter_title):
                score += 70
            elif normalized_doc_title and normalized_chapter_title and normalized_chapter_title in normalized_doc_title:
                score += 60

            if score >= 60 and self.extract_text_fields(doc):
                candidates.append((score, doc_id))

        candidates.sort(key=lambda item: (-item[0], self.doc_title(item[1], docs[item[1]]).lower(), item[1]))
        return [doc_id for _, doc_id in candidates[:5]]

    def _summary_prompt(self, project_title: str, chapter_title: str) -> str:
        return (
            "Write a brief chapter summary using only the supplied source text. "
            "Do not invent events, motivations, or names that are not explicitly present. "
            "If the source is incomplete, say so directly. "
            f"Project: {project_title}. Chapter: {chapter_title}. "
            "Return 3-5 sentences followed by a short bullet list of named characters or entities explicitly mentioned."
        )

    def extract_text_fields(self, doc: dict[str, Any]) -> dict[str, str]:
        extracted: dict[str, str] = {}
        for field_name in TEXT_FIELD_NAMES:
            value = text_value_to_plain_text(doc.get(field_name))
            if value.strip():
                extracted[field_name] = value
        return extracted

    def _descendants_in_order(self, docs: dict[str, dict[str, Any]], doc_id: str) -> list[str]:
        ordered: list[str] = []
        for child_id in docs.get(doc_id, {}).get("children", []):
            if child_id not in docs:
                continue
            ordered.append(child_id)
            ordered.extend(self._descendants_in_order(docs, child_id))
        return ordered

    def _normalize_title(self, value: str) -> str:
        return " ".join(value.lower().replace(":", " ").replace("-", " ").split())

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

    def _manuscript_doc_ids(self, docs: dict[str, dict[str, Any]]) -> set[str]:
        manuscript_node = docs.get("manuscript")
        if manuscript_node and manuscript_node.get("type") == "novel_manuscript":
            ids = {"manuscript"}
            ids.update(self._descendants_in_order(docs, "manuscript"))
            return ids

        # Fallback for projects that do not expose an explicit manuscript node.
        return {
            doc_id
            for doc_id, doc in docs.items()
            if str(doc.get("type", "")).startswith("novel_") and doc.get("type") != "novel_notes"
        }

    def search_text(self, project_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        needle = query.lower().strip()
        if not needle:
            return []

        project = self.get_project(project_id)
        docs = project.state.get("docs", {})
        manuscript_ids = self._manuscript_doc_ids(docs)
        matches: list[dict[str, Any]] = []
        for doc_id, doc in docs.items():
            if doc_id not in manuscript_ids:
                continue
            for field_name, value in self.extract_text_fields(doc).items():
                lowered = value.lower()
                index = lowered.find(needle)
                if index == -1:
                    continue
                start = max(0, index - 100)
                end = min(len(value), index + len(query) + 100)
                matches.append(
                    {
                        "doc_id": doc_id,
                        "doc_type": doc.get("type"),
                        "title": self.doc_title(doc_id, doc),
                        "field": field_name,
                        "snippet": value[start:end].replace("\n", " ").strip(),
                    }
                )
                break
            if len(matches) >= limit:
                break
        return matches