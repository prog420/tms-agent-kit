"""Command-line entry point.

Exit codes:
  0  success, nothing pending
  1  changes pending (diff found changes; push printed a plan to confirm)
  2  error, conflict or invalid usage
  130 interrupted
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

from tms_agent_kit import __version__, agent_skills, scaffold
from tms_agent_kit.config import Config, ConfigError, find_config, load_config, load_token
from tms_agent_kit.model import Case
from tms_agent_kit.plan_sync import (
    PlanChanges,
    check_plans_remote,
    describe,
    list_plans,
    plan_changes,
    push_plans,
)
from tms_agent_kit.report import NOT_RUN, build_report, find_run, write_report
from tms_agent_kit.run_sync import (
    check_run_remote,
    describe_run,
    new_run,
    outcome_summary,
    plan_run,
    push_run,
)
from tms_agent_kit.runs import OUTCOMES
from tms_agent_kit.sync import (
    Change,
    Create,
    CreateSection,
    Delete,
    Event,
    Plan,
    SyncError,
    Update,
    check_remote,
    load_state,
    plan,
    pull,
    push,
)
from tms_agent_kit.testit import TestItClient, TestItError
from tms_agent_kit.workspace import Workspace, section_label, section_paths

EXIT_OK = 0
EXIT_CHANGES = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130

ClientFactory = Callable[[Config, str], TestItClient]


def _default_client(config: Config, token: str) -> TestItClient:
    return TestItClient(config.base_url, token)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tms-agent-kit",
        description="Sync test cases between a TMS and Markdown files.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        help="path to tms-agent-kit.toml (default: search the current directory and parents)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log HTTP requests and retries to stderr"
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser(
        "check",
        help="verify config, token and API access (read-only)",
        description="Verify config, token and API access. Makes read-only requests.",
    )
    pull_parser = commands.add_parser(
        "pull",
        help="download cases from the TMS into Markdown files",
        description="Download cases into Markdown files. Local edits are kept.",
    )
    pull_parser.add_argument(
        "--force",
        action="store_true",
        help="discard local edits of cases that changed in the TMS",
    )
    diff_parser = commands.add_parser(
        "diff",
        help="show local changes against the last pull (dry-run)",
        description="Show local changes against the last pull. Exit code 1 means changes.",
    )
    diff_parser.add_argument(
        "--remote", action="store_true", help="also check the TMS for conflicting changes"
    )
    push_parser = commands.add_parser(
        "push",
        help="upload local changes to the TMS after confirmation",
        description=(
            "Without --confirm, print the plan and a confirmation token; nothing is written. "
            "With --confirm TOKEN, apply the plan if it is unchanged."
        ),
    )
    push_parser.add_argument("--confirm", metavar="TOKEN", help="apply the plan with this token")
    push_parser.add_argument(
        "--allow-delete",
        action="store_true",
        help="archive TMS cases whose files were deleted locally",
    )
    plan_parser = commands.add_parser(
        "plan",
        help="test plans: list, diff, push",
        description="Test plans described as Markdown files under plans_root.",
    )
    plan_commands = plan_parser.add_subparsers(dest="plan_command", required=True, metavar="ACTION")
    plan_commands.add_parser(
        "list",
        help="list test plans in the TMS (read-only)",
        description="List test plans of the project and the local files that manage them.",
    )
    plan_commands.add_parser(
        "diff",
        help="show plan changes against the last plan push (dry-run, offline)",
        description="Show what `plan push` would change. Exit code 1 means changes.",
    )
    plan_push = plan_commands.add_parser(
        "push",
        help="create, update or archive test plans after confirmation",
        description=(
            "Without --confirm, print the plan changes and a confirmation token; nothing is "
            "written. With --confirm TOKEN, apply them if they are unchanged."
        ),
    )
    plan_push.add_argument("--confirm", metavar="TOKEN", help="apply the changes with this token")
    plan_push.add_argument(
        "--allow-delete",
        action="store_true",
        help="archive TMS plans whose files were deleted locally",
    )
    run_parser = commands.add_parser(
        "run",
        help="test runs: new, push",
        description="Results of one test plan execution, in a Markdown file under runs_root.",
    )
    run_commands = run_parser.add_subparsers(dest="run_command", required=True, metavar="ACTION")
    run_new = run_commands.add_parser(
        "new",
        help="write a run file for a plan (offline)",
        description="Write a run file template with one section per case of the plan.",
    )
    run_new.add_argument("plan_file", type=Path, metavar="PLAN_FILE", help="the plan file")
    run_new.add_argument(
        "--configuration",
        metavar="NAME",
        help="configuration to run (default: the plan's first one, or the project default)",
    )
    run_push = run_commands.add_parser(
        "push",
        help="upload the results of a run file after confirmation",
        description=(
            "Without --confirm, print what would be uploaded and a confirmation token; nothing "
            "is written. With --confirm TOKEN, create the run and upload the results. "
            "Pushing the file again updates changed outcomes and comments."
        ),
    )
    run_push.add_argument("run_file", type=Path, metavar="RUN_FILE", help="the run file")
    run_push.add_argument("--confirm", metavar="TOKEN", help="upload with this token")

    report_parser = commands.add_parser(
        "report",
        help="write an HTML report of cases, plans and runs",
        description=(
            "Write one static HTML page with the cases, plans and runs of the workspace: "
            "coverage by section, latest results, evidence, and what needs attention. "
            "Built from local files; makes no network calls."
        ),
    )
    report_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="FILE",
        help="where to write the page (default: report.html, or RUN_FILE's name, in reports_root)",
    )
    report_parser.add_argument(
        "--run",
        type=Path,
        metavar="RUN_FILE",
        help="open the page on this run: a report on one test session",
    )

    init_parser = commands.add_parser(
        "init",
        help="lay out a test repository in the current folder; add a project",
        description=(
            "Write the test repository template (AGENTS.md, .gitignore, .mcp.json, pyproject.toml, "
            "projects/) and install the agent skills. Existing files are kept. With --project, "
            "also write projects/NAME/tms-agent-kit.toml."
        ),
    )
    init_parser.add_argument("--project", metavar="NAME", help="project folder name")
    init_parser.add_argument(
        "--base-url", metavar="URL", help="TMS URL, e.g. https://x.testit.software"
    )
    init_parser.add_argument("--project-id", metavar="ID", help="TMS project UUID or global id")

    skills_parser = commands.add_parser(
        "skills",
        help="agent skills: list, install",
        description="Agent skills shipped with this version of tms-agent-kit.",
    )
    skills_commands = skills_parser.add_subparsers(
        dest="skills_command", required=True, metavar="ACTION"
    )
    skills_commands.add_parser("list", help="list the bundled skills")
    skills_install = skills_commands.add_parser(
        "install",
        help="install or update the skills in a repository",
        description=(
            "Copy the skills into DIR (default: .claude/skills in the git repository root). "
            "Files changed locally are kept unless --force is given."
        ),
    )
    skills_install.add_argument("--dir", type=Path, metavar="DIR", help="target skills folder")
    skills_install.add_argument(
        "--force", action="store_true", help="overwrite skill files changed locally"
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: ClientFactory = _default_client,
) -> int:
    args = build_parser().parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
        # httpcore debug output is low-level noise; httpx logs one line per request.
        logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if args.command == "init":
            return _init(args.project, args.base_url, args.project_id)
        if args.command == "skills":
            return _skills(
                args.skills_command, getattr(args, "dir", None), getattr(args, "force", False)
            )
        config = load_config(args.config or find_config(Path.cwd()))
        ws = Workspace(config)
        if args.command == "diff" and not args.remote:
            return _diff(ws, None)
        if args.command == "plan" and args.plan_command == "diff":
            return _plan_diff(ws)
        if args.command == "report":
            return _report_html(ws, config, args.output, args.run)
        if args.command == "run" and args.run_command == "new":
            return _run_new(ws, args.plan_file, args.configuration)
        with client_factory(config, load_token(config)) as client:
            if args.command == "check":
                return _check(config, client, ws)
            if args.command == "pull":
                return _report(pull(ws, client, config, force=args.force), "Up to date.")
            if args.command == "diff":
                return _diff(ws, client)
            if args.command == "push":
                return _push(ws, client, args.confirm, args.allow_delete)
            if args.command == "plan" and args.plan_command == "list":
                return _plan_list(ws, client)
            if args.command == "plan" and args.plan_command == "push":
                return _plan_push(ws, client, args.confirm, args.allow_delete)
            if args.command == "run" and args.run_command == "push":
                return _run_push(ws, client, args.run_file, args.confirm)
    except (ConfigError, TestItError, SyncError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    raise AssertionError(f"unhandled command {args.command!r}")


def _init(project: str | None, base_url: str | None, project_id: str | None) -> int:
    given = [v for v in (project, base_url, project_id) if v]
    if given and len(given) != 3:
        raise ConfigError("--project, --base-url and --project-id go together")
    root = Path.cwd()
    for item in scaffold.init_repository(root):
        print(f"{item.kind:<9} {_relative(item.path, root)}")
    if project and base_url and project_id:
        item = scaffold.add_project(root, project, base_url, project_id)
        print(f"{item.kind:<9} {_relative(item.path, root)}")
    status = _install_skills(root / agent_skills.DEFAULT_DIR, force=False, root=root)
    folder = f"projects/{project}" if project else "projects/<name>"
    print(
        "\nNext:\n"
        "  uv sync                         # install tms-agent-kit into the repository\n"
        "  cp .env.example .env            # set TESTIT_TOKEN\n"
        + (
            ""
            if project
            else "  uv run tms-agent-kit init --project NAME --base-url URL --project-id ID\n"
        )
        + f"  cd {folder} && uv run tms-agent-kit check && uv run tms-agent-kit pull\n"
        "  fill in the Projects table in AGENTS.md"
    )
    return status


def _skills(action: str, directory: Path | None, force: bool) -> int:
    if action == "list":
        for name, skill in agent_skills.bundled_skills().items():
            print(f"{name:<16} {agent_skills.skill_description(skill)}")
        return EXIT_OK
    root = agent_skills.repository_root(Path.cwd())
    return _install_skills(directory or root / agent_skills.DEFAULT_DIR, force=force, root=root)


def _install_skills(target: Path, *, force: bool, root: Path) -> int:
    changes = agent_skills.install(target, force=force)
    for change in changes:
        _print_event(Event(change.kind, _relative(change.path, root), change.message))
    if not changes:
        print(f"Skills in {_relative(target, root)} are up to date ({__version__}).")
    return EXIT_ERROR if any(c.kind == "kept" for c in changes) else EXIT_OK


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _plan_diff(ws: Workspace) -> int:
    load_state(ws)  # plans reference pulled cases
    changes = plan_changes(ws)
    _print_plan_changes(changes, allow_delete=False)
    if changes.blocked:
        return EXIT_ERROR
    return EXIT_CHANGES if changes.changes else EXIT_OK


def _plan_push(ws: Workspace, client: TestItClient, confirm: str | None, allow_delete: bool) -> int:
    state = load_state(ws)
    full = plan_changes(ws)
    changes = full if allow_delete else full.without_deletes()
    check_plans_remote(changes, client, state)
    if confirm is None or changes.blocked:
        _print_plan_changes(full if confirm is None else changes, allow_delete=allow_delete)
        if changes.blocked:
            print("\nPlan push is blocked. Fix the errors and conflicts above.")
            return EXIT_ERROR
        if not changes.changes:
            print("Nothing to push.")
            return EXIT_OK
        command = "tms-agent-kit plan push" + (" --allow-delete" if allow_delete else "")
        print(
            f"\nNothing was written. Review the changes, then apply them with:\n\n"
            f"  {command} --confirm {changes.token()}"
        )
        return EXIT_CHANGES
    if confirm != changes.token():
        raise SyncError("the plan changes differ from what was shown; run `plan push` again")
    events = push_plans(ws, state, client, changes)
    _report(events, "Nothing to push.")
    return EXIT_ERROR if any(e.kind in ("failed", "conflict") for e in events) else EXIT_OK


def _plan_list(ws: Workspace, client: TestItClient) -> int:
    rows = list_plans(ws, load_state(ws), client)
    if not rows:
        print("No test plans.")
    for tms_plan, local in rows:
        build = f"  build {tms_plan['build']}" if tms_plan.get("build") else ""
        where = f"  <- {local}" if local else ""
        status = tms_plan.get("status", "?")
        print(f"#{tms_plan.get('globalId')}  {status:<10} {tms_plan.get('name')}{build}{where}")
    return EXIT_OK


def _report_html(ws: Workspace, config: Config, output: Path | None, run: Path | None) -> int:
    report = build_report(ws, config)
    focus = None
    if run is not None:
        focus = find_run(report, run)
        if focus is None:
            raise SyncError(f"{run}: not a run file in {ws.display(config.runs_root)}/")
        if focus.run is None:
            raise SyncError(f"{ws.display(focus.path)}: {focus.error}")
    name = f"{focus.path.stem}.html" if focus is not None else "report.html"
    path = (output or config.reports_root / name).resolve()
    write_report(report, path, focus)
    cases = report.runnable
    last = Counter(c.last_outcome for c in cases)
    totals = ", ".join(f"{last[o]} {o.lower()}" for o in (*OUTCOMES, NOT_RUN) if last[o])
    print(f"written   {ws.display(path)}")
    print(
        f"{len(cases)} cases ({totals or 'no results'}), {len(report.plans)} plans, "
        f"{len(report.runs)} runs"
    )
    for problem in report.problems:
        _print_event(Event("error", problem.target, problem.message))
    return EXIT_OK


def _run_new(ws: Workspace, plan_file: Path, configuration: str | None) -> int:
    load_state(ws)  # plans reference pulled cases
    path = new_run(ws, plan_file, configuration)
    print(f"created   {ws.display(path)}")
    print("\nFill in a 'Result:' for each executed case, then run:")
    print(f"\n  tms-agent-kit run push {ws.display(path)}")
    return EXIT_OK


def _run_push(ws: Workspace, client: TestItClient, run_file: Path, confirm: str | None) -> int:
    state = load_state(ws)
    run_plan = plan_run(ws, run_file)
    if not run_plan.blocked:
        check_run_remote(run_plan, client, state, ws)
    if confirm is None or run_plan.blocked:
        for event in run_plan.errors + run_plan.conflicts:
            _print_event(event)
        for event in describe_run(run_plan):
            _print_event(event)
        print(f"\nTotal: {outcome_summary(run_plan)}")
        if run_plan.blocked:
            print("\nRun push is blocked. Fix the errors and conflicts above.")
            return EXIT_ERROR
        if not run_plan.cases:
            print("Nothing to push.")
            return EXIT_OK
        print(
            f"\nNothing was written. Review the results, then upload them with:\n\n"
            f"  tms-agent-kit run push {ws.display(run_plan.path)} --confirm {run_plan.token()}"
        )
        return EXIT_CHANGES
    if confirm != run_plan.token():
        raise SyncError("the run differs from what was shown; run `run push` again")
    events = push_run(ws, state, client, run_plan)
    _report(events, "Nothing to push.")
    return EXIT_ERROR if any(e.kind in ("failed", "conflict", "error") for e in events) else EXIT_OK


def _print_plan_changes(changes: PlanChanges, *, allow_delete: bool) -> None:
    for event in changes.errors:
        _print_event(event)
    totals: Counter[str] = Counter()
    for change in changes.changes:
        totals[change.kind] += 1
        _print_event(Event(change.kind, change.target, describe(change, allow_delete=allow_delete)))
        if change.conflict:
            _print_event(Event("conflict", change.target, change.conflict))
    for event in changes.warnings:
        _print_event(event)
    if changes.changes:
        nouns = {"create": "new plan", "update": "update", "delete": "delete"}
        parts = [_count(totals[k], noun) for k, noun in nouns.items() if totals[k]]
        if totals["delete"] and not allow_delete:
            parts[-1] += " (ignored)"
        print(f"\nTotal: {', '.join(parts)}")
    if not (changes.changes or changes.errors):
        print("No plan changes.")


def _check(config: Config, client: TestItClient, ws: Workspace) -> int:
    project = client.get_project(config.project_id)
    sections = section_paths(list(client.iter_sections(config.project_id)))
    print(f"Connected to {config.base_url}")
    print(f"Project: {project.get('name', '?')} (global id {project.get('globalId', '?')})")
    print(f"Sections: {len(sections)} (folder: TMS section)")
    rows = [(s.path, section_label(sections, i)) for i, s in sections.items()]
    known = {s.path for s in sections.values()}
    rows += [(path, note) for path, note in _pending_folders(ws, known)]
    width = max((len(ws.display(ws.root / path)) + 1 for path, _ in rows), default=0)
    for path, label in sorted(rows):
        print(f"  {ws.display(ws.root / path) + '/':<{width}}  {label}")
    return EXIT_OK


def _pending_folders(ws: Workspace, known: set[str]) -> list[tuple[str, str]]:
    """Local folders that are not sections yet, with what push does with them."""
    if not ws.root.is_dir():
        return []
    result = []
    for directory in ws.root.rglob("*"):
        relative = directory.relative_to(ws.root)
        if not directory.is_dir() or any(p.startswith(("_", ".")) for p in relative.parts):
            continue
        path = relative.as_posix()
        if path in known:
            continue
        has_cases = any(
            not any(p.startswith(("_", ".")) for p in f.relative_to(directory).parts)
            for f in directory.rglob("*.md")
        )
        note = "(new: push creates this section)" if has_cases else "(not a section; empty)"
        result.append((path, note))
    return result


def _diff(ws: Workspace, client: TestItClient | None) -> int:
    state = load_state(ws)
    change_plan = plan(ws, state)
    if client is not None:
        check_remote(change_plan, client)
    _print_plan(change_plan, allow_delete=False)
    if change_plan.blocked:
        return EXIT_ERROR
    return EXIT_CHANGES if change_plan.changes else EXIT_OK


def _push(ws: Workspace, client: TestItClient, confirm: str | None, allow_delete: bool) -> int:
    state = load_state(ws)
    full_plan = plan(ws, state)
    change_plan = full_plan if allow_delete else full_plan.without_deletes()
    check_remote(change_plan, client)
    if confirm is None:
        _print_plan(full_plan, allow_delete=allow_delete, diffs=False)
        if change_plan.blocked:
            print("\nPush is blocked. Fix the errors and conflicts above.")
            return EXIT_ERROR
        if not change_plan.changes:
            print("Nothing to push.")
            return EXIT_OK
        command = "tms-agent-kit push" + (" --allow-delete" if allow_delete else "")
        print(
            "\nNothing was written. Review the plan (`tms-agent-kit diff` shows the full "
            f"changes), then apply it with:\n\n  {command} --confirm {change_plan.token()}"
        )
        return EXIT_CHANGES
    if change_plan.blocked:
        _print_plan(change_plan, allow_delete=allow_delete, diffs=False)
        print("\nPush is blocked. Fix the errors and conflicts above.")
        return EXIT_ERROR
    if confirm != change_plan.token():
        raise SyncError("the plan changed since it was shown; run `push` again and review it")
    events = push(ws, state, client, change_plan)
    _report(events, "Nothing to push.")
    failed = any(e.kind in ("failed", "conflict", "error") for e in events)
    return EXIT_ERROR if failed else EXIT_OK


def _print_plan(change_plan: Plan, *, allow_delete: bool, diffs: bool = True) -> None:
    for event in change_plan.errors:
        _print_event(event)
    totals: Counter[str] = Counter()
    for change in change_plan.changes:
        kind, detail = _describe(change, allow_delete=allow_delete)
        totals[kind] += 1
        _print_event(Event(kind, change.target, detail))
        if change.conflict:
            _print_event(Event("conflict", change.target, change.conflict))
    for event in change_plan.warnings:
        _print_event(event)
    if change_plan.changes:
        print(f"\nTotal: {_totals(totals, allow_delete=allow_delete)}")
    texts = [c.diff for c in change_plan.changes if isinstance(c, Update | Create) and c.diff]
    if diffs and texts:
        print()
        print("".join(texts), end="")
    if not (change_plan.changes or change_plan.errors):
        print("No local changes.")


def _describe(change: Change, *, allow_delete: bool) -> tuple[str, str]:
    """Plan line kind and detail. Kinds: section, create, move, update, delete."""
    if isinstance(change, CreateSection):
        return "section", f"new section {change.section!r}"
    if isinstance(change, Delete):
        return "delete", "" if allow_delete else "deleted locally; ignored without --allow-delete"
    counts = _step_counts(change.local.case)
    if isinstance(change, Create):
        return "create", f"section {change.section!r}, {counts}"
    fields = [f for f in change.fields if f != "section"]
    if change.from_section is not None:
        where = f"from {change.from_section!r} to {change.section!r}"
    else:
        where = f"section {change.section!r}"
    parts = [", ".join(fields)] if fields else []
    parts.append(f"{where}, {counts}")
    return ("update" if fields else "move"), "; ".join(parts)


_TOTAL_NOUNS = {
    "section": "new section",
    "create": "new case",
    "move": "move",
    "update": "update",
    "delete": "delete",
}


def _totals(totals: Counter[str], *, allow_delete: bool) -> str:
    parts = [_count(totals[kind], noun) for kind, noun in _TOTAL_NOUNS.items() if totals[kind]]
    if totals["delete"] and not allow_delete:
        parts[-1] += " (ignored)"
    return ", ".join(parts)


def _step_counts(case: Case) -> str:
    parts = [_count(len(case.steps), "step")]
    if case.preconditions:
        parts.append(_count(len(case.preconditions), "precondition"))
    if case.postconditions:
        parts.append(_count(len(case.postconditions), "postcondition"))
    return ", ".join(parts)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _report(events: list[Event], empty: str) -> int:
    for event in events:
        _print_event(event)
    if not events:
        print(empty)
    return EXIT_ERROR if any(e.kind in ("error", "conflict") for e in events) else EXIT_OK


def _print_event(event: Event) -> None:
    line = f"{event.kind:<9} {event.target}"
    if event.message:
        line += f"  ({event.message})"
    print(line)
