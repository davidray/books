from __future__ import annotations

import argparse
import json
from pathlib import Path

from .defaults import (
    get_export_default,
    get_project_default,
    load_defaults,
    set_default,
    set_base_url_default,
    set_model_default,
)
from .export_loader import DabbleExport
from .mcp_server import DabbleMcpServer
from .tasks import (
    cleanup_successful_tasks,
    compile_story_brief,
    get_task_status,
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



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query Dabble exports and expose grounded story tools.")
    parser.add_argument("--export", required=False, help="Path to the Dabble export JSON file (uses default if not provided)")
    parser.add_argument(
        "--project",
        help="Project ID or exact project title. Can replace positional project_id in most commands (uses default if not provided).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-projects", help="List projects in the export")
    
    subparsers.add_parser("set-defaults", help="Set default export, project, model, and/or base-url").add_argument(
        "arg",
        nargs="*",
        help="Set defaults: 'export <path>', 'project <project_id>', 'model <model_name>', 'base-url <url>', or combinations",
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
    tasks_parser.add_argument("arg1")
    tasks_parser.add_argument("arg2", nargs="?")

    run_tasks_parser = subparsers.add_parser(
        "run-summary-tasks",
        help="Execute generated summary tasks and write grounded result files",
    )
    run_tasks_parser.add_argument("output_dir")
    run_tasks_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing chapter results")
    run_tasks_parser.add_argument("--limit", type=int, help="Process only the first N pending tasks")
    run_filter_group = run_tasks_parser.add_mutually_exclusive_group()
    run_filter_group.add_argument("--pending-only", action="store_true", help="Run only tasks currently marked pending")
    run_filter_group.add_argument("--failed-only", action="store_true", help="Run only tasks currently marked failed")

    status_parser = subparsers.add_parser("task-status", help="Show per-task status for a generated task directory")
    status_parser.add_argument("output_dir")
    status_filter_group = status_parser.add_mutually_exclusive_group()
    status_filter_group.add_argument("--pending-only", action="store_true", help="Show only pending tasks")
    status_filter_group.add_argument("--failed-only", action="store_true", help="Show only failed tasks")
    status_filter_group.add_argument("--completed-only", action="store_true", help="Show only completed tasks")

    cleanup_parser = subparsers.add_parser(
        "cleanup-successful-tasks",
        help="Delete completed task packet files, optionally deleting result files too",
    )
    cleanup_parser.add_argument("output_dir")
    cleanup_parser.add_argument("--remove-results", action="store_true", help="Also delete completed result files")
    cleanup_parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without changing files")

    brief_parser = subparsers.add_parser("compile-brief", help="Combine saved chapter summaries into a story brief")
    brief_parser.add_argument("output_dir")

    subparsers.add_parser("serve", help="Run the MCP server over stdio")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Handle set-defaults command before loading exports
    if args.command == "set-defaults":
        return handle_set_defaults(args)

    # Handle list-defaults command before loading exports
    if args.command == "list-defaults":
        return handle_list_defaults()

    # For other commands, resolve export and project
    export_path_str = args.export or get_export_default()
    if not export_path_str:
        parser.error("--export is required or must be set via set-defaults command")

    export_path = resolve_export_path(export_path_str)
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
        if args.arg2 is None:
            if not args.project and not project_arg:
                parser.error(
                    "build-summary-tasks requires <project_id> <output_dir> or --project <project> <output_dir> or set default project"
                )
            project_ref = args.project or project_arg
            output_dir = args.arg1
        else:
            project_ref = args.project or project_arg or args.arg1
            output_dir = args.arg2
        project_id = resolve_project_id(export_data, project_ref)
        print(
            json.dumps(
                write_chapter_summary_tasks(export_data, project_id, Path(output_dir)),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "compile-brief":
        print(json.dumps(compile_story_brief(Path(args.output_dir)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "run-summary-tasks":
        status_filter = None
        if args.pending_only:
            status_filter = "pending"
        elif args.failed_only:
            status_filter = "failed"
        print(
            json.dumps(
                run_summary_tasks(
                    Path(args.output_dir),
                    overwrite=args.overwrite,
                    limit=args.limit,
                    status_filter=status_filter,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "task-status":
        status_filter = None
        if args.pending_only:
            status_filter = "pending"
        elif args.failed_only:
            status_filter = "failed"
        elif args.completed_only:
            status_filter = "completed"
        print(
            json.dumps(
                get_task_status(Path(args.output_dir), status_filter=status_filter),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "cleanup-successful-tasks":
        print(
            json.dumps(
                cleanup_successful_tasks(
                    Path(args.output_dir),
                    remove_results=args.remove_results,
                    dry_run=args.dry_run,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "serve":
        return DabbleMcpServer(export_path).run()

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())