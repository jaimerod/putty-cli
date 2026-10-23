#!/usr/bin/env python3
"""
putty.py

Reads PuTTY .reg export → fuzzy searchable menu → ssh connect
Handles .ppk keys via puttygen (Linux version)
"""
import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.env_util import load_dotenv
from utils.menu_flow import (  # noqa: F401 - re-exported for tests/back-compat
    ACTION_BACK,
    ACTION_CONNECT,
    ACTION_COPY,
    ACTION_PING,
    build_and_connect,
    connect_to_session,
    handle_action,
    pick_action,
    pick_session_fzf,
    pick_session_interactive,
    ping_host,
    run_menu_flow,
)
from utils.putty_parse_util import file_stamp, parse_putty_reg  # noqa: F401
from utils.recent_sessions_util import load_recent_entries, load_recent_sessions
from utils.session_util import usable_sessions  # noqa: F401


def build_parser() -> argparse.ArgumentParser:
    """CLI argument parser (also drives shell completions and --help)."""
    parser = argparse.ArgumentParser(
        prog="putty-cli",
        description="PuTTY .reg export → fuzzy menu → native ssh",
        epilog=(
            "With no arguments, shows the interactive session menu. "
            "Config via env vars or .env (see .env.example): "
            "PUTTY_REG_PATH (required), PUTTY_LOG_DIR, PUTTY_PPK_CACHE_DIR."
        ),
    )
    parser.add_argument(
        "--reg", metavar="FILE", default=None,
        help="PuTTY .reg export file (default: $PUTTY_REG_PATH or .env)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="print usable session names, one per line, and exit",
    )
    parser.add_argument(
        "--connect", metavar="SESSION",
        help="connect to a session by name (no menu); errors if not found",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="build and print the ssh command, but do not run it",
    )
    parser.add_argument(
        "--no-record", action="store_true",
        help="do not add the session to recent sessions",
    )
    parser.add_argument(
        "--archived", action="store_true",
        help="also show archived sessions (names starting with 'xxx')",
    )
    parser.add_argument(
        "--flat", action="store_true",
        help="do not group 'tag:name' sessions under tag headers",
    )
    parser.add_argument(
        "--fzf", action="store_true",
        help="use fzf for session picking (if installed) instead of "
             "the built-in menu; falls back to the menu when absent",
    )
    parser.add_argument(
        "--logs", action="store_true",
        help="browse recent session logs (open in $PAGER)",
    )
    parser.add_argument(
        "--logs-dir", metavar="DIR", default=None,
        help="log directory for --logs (default: $PUTTY_LOG_DIR resolution)",
    )
    parser.add_argument(
        "--no-tmux", action="store_true",
        help="run ssh directly without tmux (no session logging); "
             "also automatic when tmux is not installed",
    )
    parser.add_argument(
        "--tui", action=argparse.BooleanOptionalAction,
        default=True,
        help="use the Textual session browser (default; persistent UI "
             "with filter + preview); --no-tui for the simple-term-menu "
             "picker. Falls back to the menu when textual is missing",
    )
    return parser


def resolve_reg_path(cli_reg: Optional[str]) -> Path:
    """Resolve the PuTTY .reg file path: --reg flag > PUTTY_REG_PATH
    (already populated from .env by load_dotenv) > error."""
    value = cli_reg or os.environ.get("PUTTY_REG_PATH")
    if not value:
        print(
            "Error: PUTTY_REG_PATH not set.\n"
            "Copy .env.example to .env and edit it, pass --reg FILE,\n"
            "or export PUTTY_REG_PATH.",
            file=sys.stderr,
        )
        sys.exit(1)
    return Path(value).expanduser().resolve()


def run_tui_browser(
    usable: Dict[str, Dict[str, Any]],
    recent_sessions: List[str],
    recent_entries: Dict[str, Dict[str, object]],
    reg_path: Path,
    group_by_tag: bool = True,
    no_record: bool = False,
    no_tmux: bool = False,
    dry_run: bool = False,
) -> Optional[int]:
    """Run the Textual browser. Returns the process exit code, or None
    when textual isn't installed (caller falls back to the menu)."""
    try:
        from utils.tui_browser import SessionBrowser
    except ImportError:
        print("Note: textual not installed; using the standard menu "
              "(pip install 'putty-cli[tui]' for the browser UI).",
              file=sys.stderr)
        return None

    recent_file = Path.home() / ".putty_recent_sessions"
    use_tmux: Optional[bool] = False if no_tmux else None

    def connect(name: str) -> int:
        return connect_to_session(
            name, usable[name], reg_path, recent_file,
            record=not no_record, dry_run=dry_run, use_tmux=use_tmux,
        )

    app = SessionBrowser(
        usable, recent_sessions, recent_entries, reg_path,
        group_by_tag=group_by_tag, connect_callback=connect,
        recent_file=None if no_record else recent_file,
    )
    app.run()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry: parse args, load sessions, connect or show menu."""
    args = build_parser().parse_args(argv)

    dotenv_path = load_dotenv()
    if dotenv_path is None and args.reg is None and "PUTTY_REG_PATH" not in os.environ:
        print("Note: no .env file found (see .env.example).\n", file=sys.stderr)
    reg_path = resolve_reg_path(args.reg)
    recent_file = Path.home() / ".putty_recent_sessions"

    try:
        sessions: Dict[str, Dict[str, Any]] = parse_putty_reg(reg_path)
    except FileNotFoundError:
        print(f"Error: .reg file not found: {reg_path}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Error: cannot read .reg file {reg_path}: {e}", file=sys.stderr)
        return 1

    usable = usable_sessions(sessions, include_archived=args.archived)

    # Non-interactive modes
    if args.list:
        for name in sorted(usable):
            print(name)
        return 0

    if args.logs:
        from utils.logs_util import pick_and_view_log
        rc = pick_and_view_log(
            Path(args.logs_dir).expanduser() if args.logs_dir
            else reg_path.parent)
        return rc if rc is not None else 0

    if args.connect is not None:
        if args.connect not in usable:
            print(f"Error: no session named {args.connect!r}.", file=sys.stderr)
            known = "\n".join(f"  {n}" for n in sorted(usable)[:20])
            print(f"Available sessions:\n{known}", file=sys.stderr)
            return 1
        return connect_to_session(
            args.connect, usable[args.connect], reg_path, recent_file,
            record=not args.no_record, dry_run=args.dry_run,
            use_tmux=False if args.no_tmux else None,
        )

    # Interactive menu
    if not sessions:
        print("No sessions found in .reg file.", file=sys.stderr)
        return 1
    if not usable:
        print("No usable sessions found (missing HostName).", file=sys.stderr)
        return 1

    recent_sessions: List[str] = load_recent_sessions(recent_file, usable)
    recent_entries = load_recent_entries(recent_file)

    if args.tui:
        tui_rc = run_tui_browser(
            usable, recent_sessions, recent_entries, reg_path,
            group_by_tag=not args.flat,
            no_record=args.no_record, no_tmux=args.no_tmux,
            dry_run=args.dry_run,
        )
        if tui_rc is not None:
            return tui_rc
        # textual missing -> fall through to the standard menu flow

    return run_menu_flow(
        usable, recent_file, reg_path,
        include_archived=args.archived,
        group_by_tag=not args.flat,
        use_fzf=args.fzf,
        no_record=args.no_record,
        no_tmux=args.no_tmux,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())