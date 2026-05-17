from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib import parse as urlparse
from urllib import error as urlerror
from urllib import request as urlrequest

from .defaults import get_base_url_default, get_model_default
from .export_loader import DabbleExport


def write_chapter_summary_tasks(export_data: DabbleExport, project_id: str, output_dir: str | Path) -> dict[str, Any]:
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    packets = export_data.chapter_packets(project_id)

    written_files: list[str] = []
    for index, packet in enumerate(packets, start=1):
        packet = dict(packet)
        packet["chapter_order"] = index
        file_name = f"{index:03d}-{_slug(packet['chapter_title']) or packet['chapter_id']}.json"
        file_path = target_dir / file_name
        file_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        written_files.append(str(file_path))

    manifest = {
        "project_id": project_id,
        "chapter_count": len(packets),
        "tasks": written_files,
    }
    manifest_path = target_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def save_task_result(
    output_dir: str | Path,
    chapter_id: str,
    summary: str,
    evidence: list[str] | None = None,
    *,
    chapter_title: str | None = None,
    chapter_order: int | None = None,
) -> str:
    result_dir = Path(output_dir) / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "chapter_id": chapter_id,
        "summary": summary,
        "evidence": evidence or [],
    }
    if chapter_title is not None:
        payload["chapter_title"] = chapter_title
    if chapter_order is not None:
        payload["chapter_order"] = chapter_order
    file_path = result_dir / f"{chapter_id}.json"
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(file_path)


def compile_story_brief(output_dir: str | Path) -> dict[str, Any]:
    result_dir = Path(output_dir) / "results"
    summaries: list[dict[str, Any]] = []
    if result_dir.exists():
        for file_path in sorted(result_dir.glob("*.json")):
            summaries.append(json.loads(file_path.read_text(encoding="utf-8")))

    metadata_map = _load_chapter_metadata_map(Path(output_dir))
    for summary in summaries:
        chapter_id = str(summary.get("chapter_id") or "")
        metadata = metadata_map.get(chapter_id)
        if metadata:
            summary.setdefault("chapter_title", metadata.get("chapter_title"))
            summary.setdefault("chapter_order", metadata.get("chapter_order"))

    order_map = {chapter_id: int(metadata.get("chapter_order") or 0) for chapter_id, metadata in metadata_map.items()}
    summaries.sort(key=lambda item: _summary_order_key(item, order_map))
    return {
        "chapter_summaries": summaries,
        "combined_summary": "\n\n".join(item["summary"] for item in summaries if item.get("summary")),
    }


def run_summary_tasks(
    output_dir: str | Path,
    *,
    overwrite: bool = False,
    limit: int | None = None,
    status_filter: str | None = None,
) -> dict[str, Any]:
    task_dir = Path(output_dir)
    result_dir = task_dir / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    status_path = task_dir / "task-status.json"
    status_doc = _load_status_doc(task_dir)
    _reconcile_status_tasks(task_dir, status_doc)
    _sync_pending_statuses(status_doc)

    task_files = sorted(
        file_path
        for file_path in task_dir.glob("*.json")
        if file_path.name != "manifest.json"
        and file_path.name != "task-status.json"
    )

    processed = 0
    skipped_existing = 0
    skipped_limit = 0
    skipped_filter = 0
    failed = 0
    written_files: list[str] = []

    for task_file in task_files:
        if limit is not None and processed >= limit:
            skipped_limit += 1
            continue

        packet = json.loads(task_file.read_text(encoding="utf-8"))
        chapter_id = packet.get("chapter_id")
        if not chapter_id:
            continue

        result_file = result_dir / f"{chapter_id}.json"
        task_key = task_file.name
        status_doc["tasks"].setdefault(
            task_key,
            {
                "chapter_id": chapter_id,
                "task_file": str(task_file),
                "result_file": str(result_file),
                "status": "pending",
                "updated_at": _iso_now(),
                "error": None,
            },
        )
        current_status = status_doc["tasks"][task_key].get("status", "pending")
        if status_filter and current_status != status_filter:
            skipped_filter += 1
            continue

        if result_file.exists() and not overwrite:
            skipped_existing += 1
            status_doc["tasks"][task_key]["status"] = "completed"
            status_doc["tasks"][task_key]["error"] = None
            status_doc["tasks"][task_key]["updated_at"] = _iso_now()
            continue

        try:
            summary = _build_summary(packet)
            evidence = _build_evidence(packet)
            written_path = save_task_result(
                task_dir,
                chapter_id,
                summary,
                evidence,
                chapter_title=packet.get("chapter_title"),
                chapter_order=packet.get("chapter_order"),
            )
            written_files.append(written_path)
            processed += 1
            status_doc["tasks"][task_key]["status"] = "completed"
            status_doc["tasks"][task_key]["error"] = None
            status_doc["tasks"][task_key]["updated_at"] = _iso_now()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            status_doc["tasks"][task_key]["status"] = "failed"
            status_doc["tasks"][task_key]["error"] = str(exc)
            status_doc["tasks"][task_key]["updated_at"] = _iso_now()

    _sync_pending_statuses(status_doc)
    status_doc["updated_at"] = _iso_now()
    status_path.write_text(json.dumps(status_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "task_dir": str(task_dir),
        "processed": processed,
        "failed": failed,
        "skipped_existing": skipped_existing,
        "skipped_limit": skipped_limit,
        "skipped_filter": skipped_filter,
        "applied_filter": status_filter or "all",
        "status_file": str(status_path),
        "written_files": written_files,
    }


def get_task_status(output_dir: str | Path, *, status_filter: str | None = None) -> dict[str, Any]:
    task_dir = Path(output_dir)
    status_path = task_dir / "task-status.json"
    status_doc = _load_status_doc(task_dir)
    _reconcile_status_tasks(task_dir, status_doc)
    _sync_pending_statuses(status_doc)
    status_doc["updated_at"] = _iso_now()
    status_path.write_text(json.dumps(status_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    tasks = list(status_doc["tasks"].values())
    completed = sum(1 for item in tasks if item.get("status") == "completed")
    failed = sum(1 for item in tasks if item.get("status") == "failed")
    pending = sum(1 for item in tasks if item.get("status") == "pending")
    filtered_tasks = (
        [item for item in tasks if item.get("status") == status_filter]
        if status_filter
        else tasks
    )

    return {
        "task_dir": str(task_dir),
        "status_file": str(status_path),
        "applied_filter": status_filter or "all",
        "total": len(tasks),
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "filtered_count": len(filtered_tasks),
        "tasks": sorted(filtered_tasks, key=lambda item: item.get("task_file", "")),
    }


def cleanup_successful_tasks(
    output_dir: str | Path,
    *,
    remove_results: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    task_dir = Path(output_dir)
    status_path = task_dir / "task-status.json"
    status_doc = _load_status_doc(task_dir)
    _reconcile_status_tasks(task_dir, status_doc)
    _sync_pending_statuses(status_doc)

    task_entries = status_doc.get("tasks", {})
    completed_items = [
        (task_key, item)
        for task_key, item in task_entries.items()
        if item.get("status") == "completed"
    ]

    deleted_task_files: list[str] = []
    deleted_result_files: list[str] = []
    removed_status_entries = 0

    for task_key, item in completed_items:
        task_file = Path(str(item.get("task_file", "")))
        result_file = Path(str(item.get("result_file", "")))

        if task_file.exists():
            if not dry_run:
                task_file.unlink()
            deleted_task_files.append(str(task_file))

        if remove_results and result_file.exists():
            if not dry_run:
                result_file.unlink()
            deleted_result_files.append(str(result_file))

        if not dry_run:
            task_entries.pop(task_key, None)
            removed_status_entries += 1

    manifest_path = task_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        deleted_task_set = set(deleted_task_files)
        manifest_tasks = [
            path
            for path in manifest.get("tasks", [])
            if path not in deleted_task_set
        ]
        manifest["tasks"] = manifest_tasks
        manifest["chapter_count"] = len(manifest_tasks)
        if not dry_run:
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if not dry_run:
        status_doc["updated_at"] = _iso_now()
        status_path.write_text(json.dumps(status_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "task_dir": str(task_dir),
        "status_file": str(status_path),
        "dry_run": dry_run,
        "remove_results": remove_results,
        "completed_found": len(completed_items),
        "task_files_deleted": len(deleted_task_files),
        "result_files_deleted": len(deleted_result_files),
        "status_entries_removed": removed_status_entries,
        "deleted_task_files": deleted_task_files,
        "deleted_result_files": deleted_result_files,
    }


def _build_summary(packet: dict[str, Any]) -> str:
    """Build a grounded summary from packet data.

    Uses a prompt-based model call when OpenAI-compatible credentials are present.
    Falls back to a deterministic summary only when no model credentials exist,
    or when explicitly forced with DABBLE_FORCE_FALLBACK_SUMMARY=1.
    """
    force_fallback = os.getenv("DABBLE_FORCE_FALLBACK_SUMMARY", "").strip().lower() in {"1", "true", "yes"}
    if force_fallback:
        return _build_balanced_summary(packet)

    base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("DABBLE_OPENAI_BASE_URL") or get_base_url_default()
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("DABBLE_OPENAI_API_KEY")
    if api_key or _is_local_base_url(base_url):
        return _build_prompt_summary(packet, api_key=api_key, base_url=base_url)
    return _build_balanced_summary(packet)


def _build_prompt_summary(
    packet: dict[str, Any],
    *,
    api_key: str | None,
    base_url: str | None = None,
) -> str:
    source_text = str(packet.get("combined_source_text", "")).strip()
    if not source_text:
        return _insufficient_source_message(packet)

    prompt_template = str(packet.get("prompt_template", "")).strip()
    if not prompt_template:
        prompt_template = (
            "Write a brief chapter summary using only the supplied source text. "
            "Do not invent events, motivations, or names that are not explicitly present. "
            "Return 3-5 sentences followed by a short bullet list of named characters or entities explicitly mentioned."
        )

    model = os.getenv("DABBLE_SUMMARY_MODEL") or get_model_default() or "gpt-5.4-mini"
    resolved_base_url = base_url or os.getenv("OPENAI_BASE_URL") or os.getenv("DABBLE_OPENAI_BASE_URL") or get_base_url_default() or "https://api.openai.com/v1"
    endpoint = f"{resolved_base_url.rstrip('/')}/chat/completions"

    cleaned_source = _strip_source_labels(source_text)
    chunk_size_raw = os.getenv("DABBLE_SUMMARY_CHUNK_CHARS", "18000").strip()
    try:
        chunk_size = max(6000, int(chunk_size_raw))
    except ValueError:
        chunk_size = 18000

    chunks = _split_text_for_summary(cleaned_source, chunk_size)
    if len(chunks) == 1:
        return _chat_completion(
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            system_prompt="You summarize manuscript packets. Stay grounded in supplied evidence.",
            user_prompt=f"{prompt_template}\n\nSOURCE TEXT:\n{chunks[0]}",
        )

    chunk_notes: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        note = _chat_completion(
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            system_prompt="You extract grounded plot notes from manuscript text.",
            user_prompt=(
                "Read the source chunk and return 4-6 concise bullet points of concrete events,"
                " decisions, and reveals from this chunk only."
                " Do not invent details and do not add conclusions outside the chunk."
                f"\n\nCHUNK {index}/{len(chunks)}:\n{chunk}"
            ),
        )
        chunk_notes.append(f"Chunk {index}:\n{note}")

    combined_notes = "\n\n".join(chunk_notes)
    return _chat_completion(
        endpoint=endpoint,
        api_key=api_key,
        model=model,
        system_prompt="You summarize manuscript packets. Stay grounded in supplied evidence.",
        user_prompt=(
            f"{prompt_template}\n\n"
            "Use the grounded notes from all chunks to produce one coherent chapter summary."
            " Ensure the middle and ending events are represented, not just the opening."
            " Do not invent facts.\n\n"
            f"GROUNDED CHUNK NOTES:\n{combined_notes}"
        ),
    )


def _chat_completion(
    *,
    endpoint: str,
    api_key: str | None,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> str:
    body = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urlrequest.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Summary model HTTP error {exc.code}: {error_body}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"Summary model request failed: {exc}") from exc

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("Summary model returned no choices")
    message = (choices[0].get("message") or {}).get("content", "")
    summary = str(message).strip()
    if not summary:
        raise RuntimeError("Summary model returned empty content")
    return summary


def _is_local_base_url(base_url: str | None) -> bool:
    if not base_url:
        return False

    parsed = urlparse.urlparse(base_url)
    hostname = (parsed.hostname or "").lower()
    return hostname in {"localhost", "127.0.0.1", "::1"}


def _split_text_for_summary(text: str, chunk_size: int) -> list[str]:
    if len(text) <= chunk_size:
        return [text]

    paragraphs = [part.strip() for part in text.split("\n") if part.strip()]
    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for paragraph in paragraphs:
        paragraph_len = len(paragraph) + (1 if current_parts else 0)
        if current_parts and current_len + paragraph_len > chunk_size:
            chunks.append("\n".join(current_parts))
            current_parts = [paragraph]
            current_len = len(paragraph)
        elif not current_parts and len(paragraph) > chunk_size:
            for start in range(0, len(paragraph), chunk_size):
                chunks.append(paragraph[start : start + chunk_size])
            current_parts = []
            current_len = 0
        else:
            current_parts.append(paragraph)
            current_len += paragraph_len

    if current_parts:
        chunks.append("\n".join(current_parts))

    return chunks or [text]


def _build_balanced_summary(packet: dict[str, Any]) -> str:
    source_text = str(packet.get("combined_source_text", "")).strip()
    if not source_text:
        return _insufficient_source_message(packet)

    cleaned_source = _strip_source_labels(source_text)
    cleaned = re.sub(r"\s+", " ", cleaned_source)
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", cleaned) if sentence.strip()]
    if not sentences:
        return _insufficient_source_message(packet)

    selected: list[str] = []
    if len(sentences) <= 5:
        selected = sentences
    else:
        n = len(sentences)
        # Pick a spread across the chapter so we do not collapse to lead sentences.
        indices = [n // 6, n // 3, n // 2, (2 * n) // 3, (5 * n) // 6]
        for idx in indices:
            sentence = sentences[idx].strip()
            if sentence and sentence not in selected:
                selected.append(sentence)
        if len(selected) < 3:
            for sentence in sentences:
                if sentence not in selected:
                    selected.append(sentence)
                if len(selected) >= 3:
                    break

    if not selected:
        return _insufficient_source_message(packet)

    summary_sentences = selected[:5]
    entities = _extract_named_entities(source_text)
    bullets = "\n".join(f"- {name}" for name in entities[:8])
    if bullets:
        return f"{' '.join(summary_sentences)}\n\n{bullets}"
    return " ".join(summary_sentences)


def _strip_source_labels(text: str) -> str:
    # Remove packet field labels like [Ab12:body] to keep summaries natural.
    return re.sub(r"\[[^\]\n]+:[^\]\n]+\]\s*", "", text)


def _extract_named_entities(text: str) -> list[str]:
    seen: set[str] = set()
    entities: list[str] = []
    # Basic heuristic for proper names and titled objects without external NLP deps.
    for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b", text):
        candidate = match.group(1).strip()
        if len(candidate) < 2:
            continue
        if candidate in {"The", "A", "An", "And", "But", "Or", "If", "For"}:
            continue
        if candidate not in seen:
            seen.add(candidate)
            entities.append(candidate)
    return entities


def _insufficient_source_message(packet: dict[str, Any]) -> str:
    chapter_title = packet.get("chapter_title") or packet.get("chapter_id") or "Unknown chapter"
    return (
        f"Insufficient manuscript source text was available for '{chapter_title}'. "
        "No grounded summary could be generated from this packet."
    )


def _build_evidence(packet: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    for source_doc in packet.get("source_docs", []):
        doc_id = source_doc.get("id", "unknown")
        fields = source_doc.get("fields", {})
        if not isinstance(fields, dict):
            continue
        for field_name, field_value in fields.items():
            text = str(field_value).strip()
            if not text:
                continue
            snippet = re.sub(r"\s+", " ", text)[:240]
            evidence.append(f"[{doc_id}:{field_name}] {snippet}")
            if len(evidence) >= 5:
                return evidence
    return evidence


def _load_chapter_order_map(output_dir: Path) -> dict[str, int]:
    metadata_map = _load_chapter_metadata_map(output_dir)
    return {chapter_id: int(metadata.get("chapter_order") or 0) for chapter_id, metadata in metadata_map.items()}


def _load_chapter_metadata_map(output_dir: Path) -> dict[str, dict[str, Any]]:
    metadata_map: dict[str, dict[str, Any]] = {}
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return metadata_map

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return metadata_map

    for index, task_path_text in enumerate(manifest.get("tasks", []), start=1):
        task_path = Path(str(task_path_text))
        if not task_path.exists():
            continue
        try:
            packet = json.loads(task_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        chapter_id = packet.get("chapter_id")
        if chapter_id and chapter_id not in metadata_map:
            metadata_map[str(chapter_id)] = {
                "chapter_order": int(packet.get("chapter_order") or index),
                "chapter_title": packet.get("chapter_title"),
            }
    return metadata_map


def _summary_order_key(item: dict[str, Any], order_map: dict[str, int]) -> tuple[int, int, str, str]:
    chapter_id = str(item.get("chapter_id") or "")
    chapter_order = item.get("chapter_order")
    if chapter_order is None:
        chapter_order = order_map.get(chapter_id)
    order_missing = 1 if chapter_order is None else 0
    return (
        order_missing,
        int(chapter_order or 0),
        str(item.get("chapter_title") or "").lower(),
        chapter_id,
    )


def _load_status_doc(task_dir: Path) -> dict[str, Any]:
    status_path = task_dir / "task-status.json"
    if status_path.exists():
        loaded = json.loads(status_path.read_text(encoding="utf-8"))
        loaded.setdefault("task_dir", str(task_dir))
        loaded.setdefault("created_at", _iso_now())
        loaded.setdefault("updated_at", _iso_now())
        loaded.setdefault("tasks", {})
        return loaded

    doc: dict[str, Any] = {
        "task_dir": str(task_dir),
        "created_at": _iso_now(),
        "updated_at": _iso_now(),
        "tasks": {},
    }

    result_dir = task_dir / "results"
    task_files = sorted(
        file_path
        for file_path in task_dir.glob("*.json")
        if file_path.name not in {"manifest.json", "task-status.json"}
    )
    for task_file in task_files:
        packet = json.loads(task_file.read_text(encoding="utf-8"))
        chapter_id = packet.get("chapter_id")
        if not chapter_id:
            continue
        result_file = result_dir / f"{chapter_id}.json"
        status = "completed" if result_file.exists() else "pending"
        doc["tasks"][task_file.name] = {
            "chapter_id": chapter_id,
            "task_file": str(task_file),
            "result_file": str(result_file),
            "status": status,
            "updated_at": _iso_now(),
            "error": None,
        }
    return doc


def _reconcile_status_tasks(task_dir: Path, status_doc: dict[str, Any]) -> None:
    task_entries = status_doc.setdefault("tasks", {})
    result_dir = task_dir / "results"
    task_files = sorted(
        file_path
        for file_path in task_dir.glob("*.json")
        if file_path.name not in {"manifest.json", "task-status.json"}
    )

    for task_file in task_files:
        packet = json.loads(task_file.read_text(encoding="utf-8"))
        chapter_id = packet.get("chapter_id")
        if not chapter_id:
            continue
        result_file = result_dir / f"{chapter_id}.json"
        task_entries.setdefault(
            task_file.name,
            {
                "chapter_id": chapter_id,
                "task_file": str(task_file),
                "result_file": str(result_file),
                "status": "completed" if result_file.exists() else "pending",
                "updated_at": _iso_now(),
                "error": None,
            },
        )


def _sync_pending_statuses(status_doc: dict[str, Any]) -> None:
    for item in status_doc.get("tasks", {}).values():
        result_file = Path(str(item.get("result_file", "")))
        if result_file.exists() and item.get("status") != "failed":
            item["status"] = "completed"
            item["error"] = None
        elif not result_file.exists() and item.get("status") != "failed":
            item["status"] = "pending"


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _slug(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    return "-".join(segment for segment in cleaned.split("-") if segment)
