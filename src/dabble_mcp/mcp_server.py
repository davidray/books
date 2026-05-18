from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .db import DabbleDatabase
from .export_loader import DabbleExport
from .tasks import compile_story_brief, save_task_result, write_chapter_summary_tasks


JSONRPC_VERSION = "2.0"


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]


class DabbleMcpServer:
    def __init__(self, export_path: str | Path | None = None, db_path: str | Path | None = None):
        if db_path is not None:
            self.data: DabbleDatabase | DabbleExport = DabbleDatabase(db_path)
        elif export_path is not None:
            self.data = DabbleExport.from_file(export_path)
        else:
            raise ValueError("Either export_path or db_path must be provided")
        self.tools = {
            tool.name: tool
            for tool in [
                ToolSpec(
                    name="list_projects",
                    description="List projects available in the Dabble export.",
                    input_schema={"type": "object", "properties": {}},
                    handler=lambda _: self.data.list_projects(),
                ),
                ToolSpec(
                    name="get_project_outline",
                    description="Return the book/chapter/scene outline for a project.",
                    input_schema={
                        "type": "object",
                        "properties": {"project_id": {"type": "string"}},
                        "required": ["project_id"],
                    },
                    handler=lambda arguments: self.data.build_outline(arguments["project_id"]),
                ),
                ToolSpec(
                    name="get_chapter_packet",
                    description="Return a grounded chapter packet with source text and a summary prompt.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "string"},
                            "chapter_id": {"type": "string"},
                        },
                        "required": ["project_id", "chapter_id"],
                    },
                    handler=lambda arguments: self.data.chapter_packet(arguments["project_id"], arguments["chapter_id"]),
                ),
                ToolSpec(
                    name="search_project_text",
                    description="Search reconstructed manuscript text inside a project.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "string"},
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                        "required": ["project_id", "query"],
                    },
                    handler=lambda arguments: self.data.search_text(arguments["project_id"], arguments["query"], arguments.get("limit", 20)),
                ),
                ToolSpec(
                    name="build_chapter_summary_tasks",
                    description="Write one grounded chapter packet per chapter so multiple agent sessions can process them incrementally.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "string"},
                            "output_dir": {"type": "string"},
                        },
                        "required": ["project_id", "output_dir"],
                    },
                    handler=lambda arguments: write_chapter_summary_tasks(self.data, arguments["project_id"], arguments["output_dir"]),
                ),
                ToolSpec(
                    name="save_task_result",
                    description="Persist a chapter summary result so later sessions can compile the full novel brief.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "output_dir": {"type": "string"},
                            "chapter_id": {"type": "string"},
                            "summary": {"type": "string"},
                            "evidence": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["output_dir", "chapter_id", "summary"],
                    },
                    handler=lambda arguments: {
                        "file": save_task_result(
                            arguments["output_dir"],
                            arguments["chapter_id"],
                            arguments["summary"],
                            arguments.get("evidence"),
                        )
                    },
                ),
                ToolSpec(
                    name="compile_story_brief",
                    description="Combine saved chapter summaries into a single novel-level brief.",
                    input_schema={
                        "type": "object",
                        "properties": {"output_dir": {"type": "string"}},
                        "required": ["output_dir"],
                    },
                    handler=lambda arguments: compile_story_brief(arguments["output_dir"]),
                ),
            ]
        }

    def run(self) -> int:
        # Write status to stderr so MCP JSON-RPC traffic on stdout remains valid.
        sys.stderr.write("dabble-mcp ready\n")
        sys.stderr.flush()
        while True:
            message = self._read_message()
            if message is None:
                return 0
            if "id" not in message:
                continue
            response = self._handle_request(message)
            self._write_message(response)

    def _handle_request(self, message: dict[str, Any]) -> dict[str, Any]:
        method = message.get("method")
        params = message.get("params") or {}
        request_id = message.get("id")
        try:
            if method == "initialize":
                return self._result(
                    request_id,
                    {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "dabble-mcp", "version": "0.1.0"},
                        "capabilities": {"tools": {}},
                    },
                )
            if method == "notifications/initialized":
                return self._result(request_id, {})
            if method == "tools/list":
                tools_payload = [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.input_schema,
                    }
                    for tool in self.tools.values()
                ]
                return self._result(request_id, {"tools": tools_payload})
            if method == "tools/call":
                tool_name = params["name"]
                arguments = params.get("arguments") or {}
                tool = self.tools[tool_name]
                payload = tool.handler(arguments)
                return self._result(request_id, {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]})
            raise KeyError(f"Unsupported method: {method}")
        except Exception as exc:  # noqa: BLE001
            return {
                "jsonrpc": JSONRPC_VERSION,
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    def _read_message(self) -> dict[str, Any] | None:
        headers: dict[str, str] = {}
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            name, value = line.decode("utf-8").split(":", 1)
            headers[name.strip().lower()] = value.strip()

        length = int(headers.get("content-length", "0"))
        if length <= 0:
            return None
        body = sys.stdin.buffer.read(length)
        return json.loads(body.decode("utf-8"))

    def _write_message(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()

    def _result(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}