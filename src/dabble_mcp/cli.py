from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .defaults import (
    get_db_default,
    get_export_default,
    get_project_default,
    load_defaults,
    set_default,
    set_base_url_default,
    set_db_default,
    set_model_default,
)
from .db import DabbleDatabase
from .export_loader import DabbleExport
from .mcp_server import DabbleMcpServer
from .tasks import (
    cleanup_successful_tasks,
    compile_story_brief,
    get_task_status,
    run_summary_task_queue,
    run_summary_tasks,
    write_chapter_summary_tasks,
)


def resolve_export_path(raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.exists():
        return candidate

    local_exports = Path("Exports") / raw_path
    if local_exports.exists():
        return local_exports

    raise FileNotFoundError(
        f"Export file not found: {raw_path}. Tried '{candidate}' and '{local_exports}'."
    )


def resolve_project_id(export_data: DabbleExport, project_ref: str) -> str:
    project_ref = project_ref.strip()
    if not project_ref:
        raise ValueError("Project reference cannot be empty")

    projects = export_data.list_projects()

    by_id = {project["project_id"]: project["project_id"] for project in projects}
    if project_ref in by_id:
        return by_id[project_ref]

    exact_title_matches = [
        project["project_id"]
        for project in projects
        if str(project.get("title", "")).lower() == project_ref.lower()
    ]
    if len(exact_title_matches) == 1:
        return exact_title_matches[0]
    if len(exact_title_matches) > 1:
        raise ValueError(
            f"Project title '{project_ref}' is ambiguous; pass a project ID instead."
        )

    raise ValueError(f"Project not found: {project_ref}")


def handle_set_defaults(args: argparse.Namespace) -> int:
    """Handle the set-defaults command."""
    if not args.arg:
        print("Current defaults:")
        return handle_list_defaults()

    # Parse arguments: can be 'export <path>' or 'project <id>' or both
    defaults = load_defaults()
    i = 0
    while i < len(args.arg):
        key = args.arg[i]
        if key == "export" and i + 1 < len(args.arg):
            defaults = set_default("export", args.arg[i + 1])
            print(f"Export default set to: {args.arg[i + 1]}")
            i += 2
        elif key == "project" and i + 1 < len(args.arg):
            defaults = set_default("project", args.arg[i + 1])
            print(f"Project default set to: {args.arg[i + 1]}")
            i += 2
        elif key == "model" and i + 1 < len(args.arg):
            defaults = set_model_default(args.arg[i + 1])
            print(f"Model default set to: {args.arg[i + 1]}")
            i += 2
        elif key == "base-url" and i + 1 < len(args.arg):
            defaults = set_base_url_default(args.arg[i + 1])
            print(f"Base URL default set to: {args.arg[i + 1]}")
            i += 2
        elif key == "db" and i + 1 < len(args.arg):
            defaults = set_db_default(args.arg[i + 1])
            print(f"DB default set to: {args.arg[i + 1]}")
            i += 2
        else:
            print(f"Unknown option or missing value: {key}", file=__import__("sys").stderr)
            return 1
    return 0


def handle_list_defaults() -> int:
    """Handle the list-defaults command."""
    defaults = load_defaults()
    if not defaults:
        print("No defaults set.")
        return 0
    print(json.dumps(defaults, ensure_ascii=False, indent=2))
    return 0


def emit_queue_progress(event: dict[str, object]) -> None:
    event_type = str(event.get("event") or "")
    if event_type == "queue_started":
        total = int(event.get("total") or 0)
        print(
            f"Queueing {total} chapter task(s) from {event.get('task_dir')}",
            file=sys.stderr,
        )
        return

    index = int(event.get("index") or 0)
    total = int(event.get("total") or 0)
    chapter_order = event.get("chapter_order")
    chapter_title = str(event.get("chapter_title") or event.get("chapter_id") or "unknown chapter")
    chapter_label = f"Chapter {chapter_order}: {chapter_title}" if chapter_order else chapter_title

    if event_type == "task_started":
        print(f"[{index}/{total}] Running {chapter_label}", file=sys.stderr)
        return

    outcome = str(event.get("outcome") or "")
    if outcome == "processed":
        message = f"[{index}/{total}] Completed {chapter_label}"
    elif outcome == "failed":
        message = f"[{index}/{total}] Failed {chapter_label}: {event.get('error')}"
    else:
        message = f"[{index}/{total}] Skipped {chapter_label}"
    print(message, file=sys.stderr)



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query Dabble exports and expose grounded story tools.")
    parser.add_argument("--export", required=False, help="Path to the Dabble export JSON file (uses default if not provided)")
    parser.add_argument("--db", required=False, help="Path to the SQLite database (preferred over --export when both are present)")
    parser.add_argument(
        "--project",
        help="Project ID or exact project title. Can replace positional project_id in most commands (uses default if not provided).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-projects", help="List projects in the export")

    import_parser = subparsers.add_parser("import", help="Convert a Dabble JSON export to a SQLite database")
    import_parser.add_argument("export_file", nargs="?", help="Path to the Dabble export JSON file (uses --export default if omitted)")
    import_parser.add_argument("--db", dest="import_db", help="Output SQLite database path (default: .dabble-tasks/dabble.db)")

    subparsers.add_parser("set-defaults", help="Set default export, project, model, db, and/or base-url").add_argument(
        "arg",
        nargs="*",
        help="Set defaults: 'export <path>', 'project <project_id>', 'model <model_name>', 'base-url <url>', 'db <path>', or combinations",
    )

    subparsers.add_parser("list-defaults", help="List current default export and project")

    outline_parser = subparsers.add_parser("outline", help="Print the outline for a project")
    outline_parser.add_argument("project_ref", nargs="?")

    chapter_parser = subparsers.add_parser("chapter-packet", help="Print one grounded chapter packet")
    chapter_parser.add_argument("arg1")
    chapter_parser.add_argument("arg2", nargs="?")

    search_parser = subparsers.add_parser("search", help="Search reconstructed text in a project")
    search_parser.add_argument("arg1")
    search_parser.add_argument("arg2", nargs="?")
    search_parser.add_argument("--limit", type=int, default=20)

    tasks_parser = subparsers.add_parser("build-summary-tasks", help="Write per-chapter packets for later agent sessions")
    tasks_parser.add_argument("arg1", nargs="?")
    tasks_parser.add_argument("arg2", nargs="?")

    run_tasks_parser = subparsers.add_parser(
        "run-summary-tasks",
        help="Execute generated summary tasks and write grounded result files",
    )
    run_tasks_parser.add_argument("output_dir", nargs="?")
    run_tasks_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing chapter results")
    run_tasks_parser.add_argument(
        "--limit",
        type=int,
        help=(
            "Process only the first N matching tasks. Use 0 for no cap. "
            "If omitted, local OpenAI-compatible backends default to 1 task per run."
        ),
    )
    run_filter_group = run_tasks_parser.add_mutually_exclusive_group()
    run_filter_group.add_argument("--pending-only", action="store_true", help="Run only tasks currently marked pending")
    run_filter_group.add_argument("--failed-only", action="store_true", help="Run only tasks currently marked failed")

    queue_tasks_parser = subparsers.add_parser(
        "queue-summary-tasks",
        help="Queue summary tasks sequentially with command-line progress output",
    )
    queue_tasks_parser.add_argument("output_dir", nargs="?")
    queue_tasks_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing chapter results")
    queue_tasks_parser.add_argument(
        "--limit",
        type=int,
        help="Queue at most N matching tasks. Omit to queue all matching chapters. Use 0 for no cap.",
    )
    queue_filter_group = queue_tasks_parser.add_mutually_exclusive_group()
    queue_filter_group.add_argument("--pending-only", action="store_true", help="Queue only tasks currently marked pending")
    queue_filter_group.add_argument("--failed-only", action="store_true", help="Queue only tasks currently marked failed")

    status_parser = subparsers.add_parser("task-status", help="Show per-task status for a generated task directory")
    status_parser.add_argument("output_dir", nargs="?")
    status_filter_group = status_parser.add_mutually_exclusive_group()
    status_filter_group.add_argument("--pending-only", action="store_true", help="Show only pending tasks")
    status_filter_group.add_argument("--failed-only", action="store_true", help="Show only failed tasks")
    status_filter_group.add_argument("--completed-only", action="store_true", help="Show only completed tasks")

    cleanup_parser = subparsers.add_parser(
        "cleanup-successful-tasks",
        help="Delete completed task packet files, optionally deleting result files too",
    )
    cleanup_parser.add_argument("output_dir", nargs="?")
    cleanup_parser.add_argument("--remove-results", action="store_true", help="Also delete completed result files")
    cleanup_parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without changing files")

    brief_parser = subparsers.add_parser("compile-brief", help="Combine saved chapter summaries into a story brief")
    brief_parser.add_argument("output_dir", nargs="?")

    subparsers.add_parser("serve", help="Run the MCP server over stdio")
    return parser


def resolve_output_dir(args: argparse.Namespace, project_id: str, parser: argparse.ArgumentParser) -> Path:
    """Return the output_dir Path, defaulting to .dabble-tasks/<project_id>."""
    if args.output_dir:
        return Path(args.output_dir)
    return Path(".dabble-tasks") / project_id


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Handle set-defaults command before loading exports
    if args.command == "set-defaults":
        return handle_set_defaults(args)

    # Handle list-defaults command before loading exports
    if args.command == "list-defaults":
        return handle_list_defaults()

    # Handle import command: JSON export → SQLite
    if args.command == "import":
        export_path_str = getattr(args, "export_file", None) or args.export or get_export_default()
        if not export_path_str:
            parser.error("import requires an export file path or --export default")
        export_path = resolve_export_path(export_path_str)
        db_path_str = getattr(args, "import_db", None) or args.db or get_db_default() or str(Path(".dabble-tasks") / "dabble.db")
        db_path = Path(db_path_str)
        print(f"Importing {export_path} → {db_path} ...", file=sys.stderr)
        export_data = DabbleExport.from_file(export_path)
        DabbleDatabase.import_from_export(export_data, db_path)
        print(json.dumps({"db": str(db_path), "status": "ok"}, ensure_ascii=False, indent=2))
        return 0

    # For all other commands, load the appropriate data source.
    # Prefer --db (or db default) over --export.
    db_path_str = args.db or get_db_default()
    export_path_str = args.export or get_export_default()

    if not db_path_str and not export_path_str:
        parser.error("--db or --export is required, or set a default via set-defaults")

    if db_path_str:
        export_data: DabbleDatabase | DabbleExport = DabbleDatabase(db_path_str)
    else:
        export_path = resolve_export_path(export_path_str)  # type: ignore[arg-type]
        export_data = DabbleExport.from_file(export_path)

    # Get project with defaults
    project_arg = args.project or get_project_default()

    if args.command == "list-projects":
        print(json.dumps(export_data.list_projects(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "outline":
        project_ref = args.project or project_arg or args.project_ref
        if not project_ref:
            parser.error("outline requires project_id as a positional argument or --project or set defaults")
        project_id = resolve_project_id(export_data, project_ref)
        print(json.dumps(export_data.build_outline(project_id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "chapter-packet":
        if args.arg2 is None:
            if not args.project and not project_arg:
                parser.error("chapter-packet requires <project_id> <chapter_id> or --project <project> <chapter_id> or set default project")
            project_ref = args.project or project_arg
            chapter_id = args.arg1
        else:
            project_ref = args.project or project_arg or args.arg1
            chapter_id = args.arg2
        project_id = resolve_project_id(export_data, project_ref)
        print(json.dumps(export_data.chapter_packet(project_id, chapter_id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "search":
        if args.arg2 is None:
            if not args.project and not project_arg:
                parser.error("search requires <project_id> <query> or --project <project> <query> or set default project")
            project_ref = args.project or project_arg
            query = args.arg1
        else:
            project_ref = args.project or project_arg or args.arg1
            query = args.arg2
        project_id = resolve_project_id(export_data, project_ref)
        print(json.dumps(export_data.search_text(project_id, query, args.limit), ensure_ascii=False, indent=2))
        return 0
    if args.command == "build-summary-tasks":
        if args.arg1 is None and args.arg2 is None:
            # No positional args: need a default project; output_dir derived from project_id
            if not args.project and not project_arg:
                parser.error(
                    "build-summary-tasks requires <project_id> <output_dir> or --project <project> <output_dir> or set default project"
                )
            project_ref = args.project or project_arg
            project_id = resolve_project_id(export_data, project_ref)
            output_dir = Path(".dabble-tasks") / project_id
        elif args.arg2 is None:
            if not args.project and not project_arg:
                parser.error(
                    "build-summary-tasks requires <project_id> <output_dir> or --project <project> <output_dir> or set default project"
                )
            project_ref = args.project or project_arg
            output_dir = Path(args.arg1)
            project_id = resolve_project_id(export_data, project_ref)
        else:
            project_ref = args.project or project_arg or args.arg1
            output_dir = Path(args.arg2)
            project_id = resolve_project_id(export_data, project_ref)
        print(
            json.dumps(
                write_chapter_summary_tasks(export_data, project_id, output_dir),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "compile-brief":
        default_project_id = resolve_project_id(export_data, project_arg) if project_arg else None
        output_dir = resolve_output_dir(args, default_project_id or "", parser)
        if not args.output_dir and not default_project_id:
            parser.error("compile-brief requires output_dir or a default project set via set-defaults")
        print(json.dumps(compile_story_brief(output_dir), ensure_ascii=False, indent=2))
        return 0
    if args.command == "run-summary-tasks":
        default_project_id = resolve_project_id(export_data, project_arg) if project_arg else None
        if not args.output_dir and not default_project_id:
            parser.error("run-summary-tasks requires output_dir or a default project set via set-defaults")
        output_dir = resolve_output_dir(args, default_project_id or "", parser)
        status_filter = None
        if args.pending_only:
            status_filter = "pending"
        elif args.failed_only:
            status_filter = "failed"
        print(
            json.dumps(
                run_summary_tasks(
                    output_dir,
                    overwrite=args.overwrite,
                    limit=args.limit,
                    status_filter=status_filter,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "queue-summary-tasks":
        default_project_id = resolve_project_id(export_data, project_arg) if project_arg else None
        if not args.output_dir and not default_project_id:
            parser.error("queue-summary-tasks requires output_dir or a default project set via set-defaults")
        output_dir = resolve_output_dir(args, default_project_id or "", parser)
        status_filter = None
        if args.pending_only:
            status_filter = "pending"
        elif args.failed_only:
            status_filter = "failed"
        print(
            json.dumps(
                run_summary_task_queue(
                    output_dir,
                    overwrite=args.overwrite,
                    limit=args.limit,
                    status_filter=status_filter,
                    progress_callback=emit_queue_progress,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "task-status":
        default_project_id = resolve_project_id(export_data, project_arg) if project_arg else None
        if not args.output_dir and not default_project_id:
            parser.error("task-status requires output_dir or a default project set via set-defaults")
        output_dir = resolve_output_dir(args, default_project_id or "", parser)
        status_filter = None
        if args.pending_only:
            status_filter = "pending"
        elif args.failed_only:
            status_filter = "failed"
        elif args.completed_only:
            status_filter = "completed"
        print(
            json.dumps(
                get_task_status(output_dir, status_filter=status_filter),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "cleanup-successful-tasks":
        default_project_id = resolve_project_id(export_data, project_arg) if project_arg else None
        if not args.output_dir and not default_project_id:
            parser.error("cleanup-successful-tasks requires output_dir or a default project set via set-defaults")
        output_dir = resolve_output_dir(args, default_project_id or "", parser)
        print(
            json.dumps(
                cleanup_successful_tasks(
                    output_dir,
                    remove_results=args.remove_results,
                    dry_run=args.dry_run,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "serve":
        if db_path_str:
            return DabbleMcpServer(db_path=db_path_str).run()
        return DabbleMcpServer(export_path=export_path).run()

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())