#!/usr/bin/env python3
"""
putty.py

Reads PuTTY .reg export → fuzzy searchable menu → ssh connect
Handles .ppk keys via puttygen (Linux version)
"""
import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.display_util import generate_menu_items
from utils.env_util import load_dotenv
from utils.putty_parse_util import parse_putty_reg
from utils.recent_sessions_util import (load_recent_sessions,
                                        update_recent_sessions)
from utils.ssh_command_util import build_ssh_command
from utils.titlecase_util import unquote_display

try:
    from simple_term_menu import TerminalMenu
except ImportError:
    print("Error: simple-term-menu not installed.", file=sys.stderr)
    print("Please use a venv and install dependencies:", file=sys.stderr)
    print("  python3 -m venv .venv", file=sys.stderr)
    print("  source .venv/bin/activate", file=sys.stderr)
    print("  pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)


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
# ───────────────────────────────────────────────────────────────────────────


def usable_sessions(sessions: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Filter sessions to those with a HostName, hiding archived (xxx*) ones."""
    return {
        k: v
        for k, v in sessions.items()
        if "HostName" in v and v["HostName"].strip() and not k.lower().startswith("xxx")
        # sessions named xxx* are treated as archived and hidden from the menu
    }


def connect_to_session(
    selected_name: str,
    session: Dict[str, Any],
    reg_path: Path,
    recent_file: Path,
    record: bool = True,
    dry_run: bool = False,
) -> int:
    """Build and run (or just print) the ssh command for one session.

    Returns the process exit code (0 for dry-run)."""
    print(f"\nSelected: {unquote_display(selected_name)}")

    cmd, temp_key = build_ssh_command(
        unquote_display(selected_name), session, reg_path=reg_path
    )

    if not cmd:
        print("Cannot build ssh command (missing hostname?).", file=sys.stderr)
        return 1

    print(f"Command: {shlex.join(cmd)}\n")

    if dry_run:
        return 0

    try:
        result = subprocess.run(cmd, check=False)
        exit_code = result.returncode
    except FileNotFoundError:
        print("Error: 'ssh' or 'tmux' not found. Please install openssh-client and tmux.", file=sys.stderr)
        exit_code = 1
    else:
        # Record the session on any normal exit, including ssh failures
        # (255 = connection refused/timeout) so failed hosts stay handy
        # for a quick retry. tmux launch failure (see FileNotFoundError
        # above) is NOT recorded - the session never actually opened.
        if record:
            update_recent_sessions(recent_file, selected_name)
    finally:
        # Cached keys live in the ppk cache dir and must survive; only
        # delete keys created outside it (cache-miss fallback temp files).
        if temp_key and os.path.exists(temp_key):
            cache_root = os.environ.get("PUTTY_PPK_CACHE_DIR") or str(
                Path.home() / ".cache" / "putty-cli" / "keys"
            )
            if not os.path.abspath(temp_key).startswith(os.path.abspath(cache_root)):
                try:
                    os.unlink(temp_key)
                except OSError as e:
                    print(f"Warning: Could not remove temp key: {e}", file=sys.stderr)
    return exit_code


def pick_session_interactive(
    usable: Dict[str, Dict[str, Any]], recent_sessions: List[str]
) -> Optional[str]:
    """Show the menu; returns the selected session name or None on cancel."""
    menu_items: List[str] = []
    display_to_name: Dict[str, str] = {}
    for display, name in generate_menu_items(usable, recent_sessions):
        menu_items.append(display)
        if name:
            display_to_name[display] = name

    menu_title = (
        "\n" + "=" * 80 + "\n"
        " PuTTY Sessions  (arrows / type to filter / Enter = connect / Esc = quit)\n"
        + "=" * 80
    )

    terminal_menu = TerminalMenu(
        menu_items,
        title=menu_title,
        menu_cursor="➜ ",
        menu_cursor_style=("fg_green", "bold"),
        menu_highlight_style=("standout",),
        search_key="/",
        clear_screen=True,
        show_search_hint=True,
    )

    selected_index = terminal_menu.show()

    if selected_index is None:
        return None

    # Handle re-selection if a separator is chosen
    selected_display = menu_items[selected_index]
    while selected_display not in display_to_name:
        print("Invalid selection (separator). Please choose again.", file=sys.stderr)
        selected_index = terminal_menu.show()
        if selected_index is None:
            return None
        selected_display = menu_items[selected_index]

    return display_to_name[selected_display]


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

    usable = usable_sessions(sessions)

    # Non-interactive modes
    if args.list:
        for name in sorted(usable):
            print(name)
        return 0

    if args.connect is not None:
        if args.connect not in usable:
            print(f"Error: no session named {args.connect!r}.", file=sys.stderr)
            known = "\n".join(f"  {n}" for n in sorted(usable)[:20])
            print(f"Available sessions:\n{known}", file=sys.stderr)
            return 1
        return connect_to_session(
            args.connect, usable[args.connect], reg_path, recent_file,
            record=not args.no_record, dry_run=args.dry_run,
        )

    # Interactive menu
    if not sessions:
        print("No sessions found in .reg file.", file=sys.stderr)
        return 1
    if not usable:
        print("No usable sessions found (missing HostName).", file=sys.stderr)
        return 1

    recent_sessions: List[str] = load_recent_sessions(recent_file, usable)

    selected_name = pick_session_interactive(usable, recent_sessions)
    if selected_name is None:
        print("\nSelection cancelled.")
        return 0

    return connect_to_session(
        selected_name, usable[selected_name], reg_path, recent_file,
        record=not args.no_record, dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())