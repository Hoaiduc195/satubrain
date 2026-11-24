"""Command line entry point for local memory and integrations."""

import argparse
import getpass
import json
import os
from pathlib import Path
import sqlite3
import sys

from . import VERSION
from .integrations import AGENTS, client_config, hook_config, run_hook
from .scope import Workspace, label
from .store import KINDS, MAX_SNAPSHOT, MAX_TEXT, ROLES, Store, default_db, json_bytes
from .sync import atomic_write, pull, push, read_limited

ACTIONS = ("remember", "handoff", "record", "history-import", "history", "search", "resume", "forget")
HISTORY_LIMIT = 16 * 1024 * 1024


def print_json(value):
    print(json.dumps(value, ensure_ascii=True, indent=2))


def add_scope(parser):
    parser.add_argument("--workspace", type=Path, help="git workspace used to resolve project and branch")
    parser.add_argument("--project", help="override the portable project ID")
    parser.add_argument("--branch", help="override the current Git branch")


def build_parser():
    parser = argparse.ArgumentParser(prog="satubrain", description="Local-first memory for coding agents")
    parser.add_argument("--db", type=Path, help="database file (or SATUBRAIN_DB)")
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    definitions = {
        "remember": "Remember a note or decision", "handoff": "Save a task checkpoint",
        "record": "Save a selected conversation message", "history-import": "Import selected JSONL messages",
        "history": "Read a saved conversation in order",
        "search": "Search task memory", "resume": "Retrieve a bounded task context",
        "forget": "Delete one item from the selected scope",
    }
    sub = {}
    for name, description in definitions.items():
        sub[name] = command = commands.add_parser(name, help=description)
        add_scope(command)
        command.add_argument("--task", required=True, help="task name or issue ID")
    for name in ("remember", "handoff", "record"):
        sub[name].add_argument("text", nargs="?", help="text to save, or read from stdin")
        sub[name].add_argument("--agent", default="human")
    sub["remember"].add_argument("--kind", choices=("note", "decision"), default="note")
    sub["handoff"].add_argument("--session", default="")
    sub["record"].add_argument("--session", required=True)
    sub["record"].add_argument("--role", choices=ROLES, required=True)
    sub["record"].add_argument("--position", type=int, required=True)
    sub["history-import"].add_argument("file", type=Path)
    sub["history-import"].add_argument("--agent", default="human")
    sub["history-import"].add_argument("--session", required=True)
    sub["history"].add_argument("--session", required=True)
    sub["history"].add_argument("--after", type=int, default=-1)
    sub["history"].add_argument("--limit", type=int, default=100)
    for name in ("search", "resume"):
        sub[name].add_argument("query", default="", nargs="?")
    sub["search"].add_argument("--kind", choices=KINDS)
    sub["search"].add_argument("--limit", type=int, default=20)
    sub["resume"].add_argument("--budget", type=int, default=1600)
    sub["forget"].add_argument("id")
    commands.add_parser("scopes", help="List projects, tasks and branches")
    for name in ("export", "import"):
        commands.add_parser(name, help=f"{name.title()} a portable JSON snapshot").add_argument("file", type=Path)
    sync = commands.add_parser("sync", help="Transfer encrypted snapshots through a synced folder")
    sync.add_argument("direction", choices=("push", "pull"))
    sync.add_argument("folder", type=Path)
    for name, description in (
        ("mcp", "Serve Model Context Protocol over stdio"),
        ("dashboard", "Open the local memory dashboard"),
        ("config", "Generate client configuration"),
    ):
        sub[name] = commands.add_parser(name, help=description)
        add_scope(sub[name])
        sub[name].add_argument("--task")
    sub["mcp"].add_argument("--agent", choices=AGENTS, default="generic")
    sub["dashboard"].add_argument("--port", type=int, default=0)
    sub["dashboard"].add_argument("--all-projects", action="store_true")
    sub["dashboard"].add_argument("--no-browser", action="store_true")
    sub["config"].add_argument("agent", choices=AGENTS)
    sub["config"].add_argument("--hooks", action="store_true", help="generate opt-in lifecycle hooks")
    commands.add_parser("hook", help="Handle an installed lifecycle hook").add_argument("token")
    return parser


def history_import(store, path, prefix, agent, session):
    raw = read_limited(path, HISTORY_LIMIT).decode("utf-8")
    entries = []
    for number, line in enumerate(raw.split("\n"), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or not {"role", "text"} <= row.keys():
                raise ValueError("expected an object with role and text")
            if not set(row) <= {"role", "text", "position", "created_at"}:
                raise ValueError("unexpected message field")
            row.setdefault("position", number)
            entries.append({**prefix, "kind": "message", "agent": agent, "session": session, **row})
        except ValueError as error:
            raise ValueError(f"invalid message at line {number}: {error}") from None
    return {"added": store.save_batch(entries), "messages": len(entries)}


def task_action(args, store, workspace):
    prefix = {"project": workspace.project, "task": label(args.task, "task"), "branch": workspace.branch}
    scope = tuple(prefix.values())
    if args.command == "search":
        return store.search(*scope, args.query, args.limit, args.kind)
    if args.command == "resume":
        return store.context(*scope, args.query, args.budget)
    if args.command == "forget":
        return {"deleted": store.delete(*scope, args.id)}
    if args.command == "history":
        return store.history(*scope, args.session, args.after, args.limit)
    if args.command == "history-import":
        return history_import(store, args.file, prefix, args.agent, args.session)
    value = args.text if args.text is not None else sys.stdin.read(MAX_TEXT + 1)
    fields = {}
    if args.command == "record":
        fields = {"session": args.session, "position": args.position, "role": args.role}
    elif args.command == "handoff":
        fields = {"session": args.session}
    kind = {"remember": getattr(args, "kind", None), "handoff": "handoff", "record": "message"}
    return store.save(**prefix, agent=args.agent, kind=kind[args.command], text=value, **fields)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in (*ACTIONS, "mcp", "dashboard", "config"):
            def resolve():
                return Workspace.resolve(args.workspace, args.project, args.branch)

            workspace = resolve()
        if args.command == "config":
            result = (hook_config(args.agent, args.task, args.project, args.branch, args.db)
                      if args.hooks else client_config(args.agent, workspace, args.workspace,
                                                      args.task, args.branch, args.db))
            if isinstance(result, str):
                print(result, end="")
            else:
                print_json(result)
            return 0
        store = Store(args.db or default_db())
        if args.command in ACTIONS:
            result = task_action(args, store, workspace)
        elif args.command == "scopes":
            result = store.scopes()
        elif args.command == "export":
            destination = args.file.expanduser()
            if destination.resolve() == store.path.resolve() or destination.exists():
                raise ValueError("export requires a new file")
            atomic_write(destination, json_bytes(store.export()) + b"\n", overwrite=False)
            result = {"file": str(destination)}
        elif args.command == "import":
            result = store.merge(json.loads(read_limited(args.file.expanduser(), MAX_SNAPSHOT)))
        elif args.command == "sync":
            password = os.environ.get("SATUBRAIN_SYNC_PASSPHRASE")
            if password is None:
                if not sys.stdin.isatty():
                    raise ValueError("set SATUBRAIN_SYNC_PASSPHRASE when running without a terminal")
                password = getpass.getpass("Sync passphrase: ")
            operation = push if args.direction == "push" else pull
            result = operation(store, args.folder, password)
        elif args.command == "hook":
            run_hook(args.token, store)
            return 0
        elif args.command == "mcp":
            from .mcp import Server, serve

            serve(Server(store, resolve, args.agent, args.task))
            return 0
        elif args.command == "dashboard":
            from .dashboard import run_dashboard

            run_dashboard(store, workspace, args.task, args.port, args.no_browser, args.all_projects)
            return 0
        print_json(result)
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, sqlite3.Error, RecursionError) as error:
        print(f"satubrain: {error}", file=sys.stderr)
        return 1
