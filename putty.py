#!/usr/bin/env python3
"""
putty.py

Reads PuTTY .reg export → fuzzy searchable menu → ssh connect
Handles .ppk keys via puttygen (Linux version)
"""
import argparse
import os
import shlex
import shutil
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


def usable_sessions(
    sessions: Dict[str, Dict[str, Any]], include_archived: bool = False
) -> Dict[str, Dict[str, Any]]:
    """Filter sessions to those with a HostName; by default hides
    archived sessions (name starts with 'xxx')."""
    return {
        k: v
        for k, v in sessions.items()
        if "HostName" in v and v["HostName"].strip()
        and (include_archived or not k.lower().startswith("xxx"))
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


def pick_session_fzf(
    usable: Dict[str, Dict[str, Any]], reg_path: Path
) -> Optional[str]:
    """Pick a session via fzf with a live preview of the ssh command.
    Returns None if fzf is missing or the user cancelled."""
    if shutil.which("fzf") is None:
        print("Note: fzf not found; using built-in menu.", file=sys.stderr)
        return None

    names = sorted(usable)
    preview_cmd = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f"--connect {{}} --dry-run 2>/dev/null | head -40"
    )
    try:
        result = subprocess.run(
            ["fzf", "--height=40%", "--reverse",
             "--preview", preview_cmd, "--preview-window", "right:60%:wrap"],
            input="\n".join(names), text=True, capture_output=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None  # user pressed Esc
    selected = result.stdout.strip()
    return selected if selected in usable else None


def pick_session_interactive(
    usable: Dict[str, Dict[str, Any]], recent_sessions: List[str],
    group_by_tag: bool = True,
) -> Optional[str]:
    """Show the menu; returns the selected session name or None on cancel."""
    menu_items: List[str] = []
    display_to_name: Dict[str, str] = {}
    for display, name in generate_menu_items(
        usable, recent_sessions, group_by_tag=group_by_tag
    ):
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


ACTION_CONNECT = "Connect"
ACTION_COPY = "Copy ssh command to clipboard"
ACTION_PING = "Ping host (check reachability)"
ACTION_BACK = "Back to session list"


def pick_action(session_name: str) -> Optional[str]:
    """Show the per-session action menu. None = back to session list."""
    actions = [ACTION_CONNECT, ACTION_COPY, ACTION_PING, ACTION_BACK]
    action_menu = TerminalMenu(
        actions,
        title=f"\n Session: {unquote_display(session_name)}\n",
        menu_cursor="➜ ",
        menu_cursor_style=("fg_cyan", "bold"),
        menu_highlight_style=("standout",),
        clear_screen=False,
    )
    idx = action_menu.show()
    if idx is None:
        return None
    return actions[idx]


def copy_to_clipboard(text: str) -> bool:
    """Copy text via OSC52 (any terminal) or xclip/wl-copy fallback."""
    import base64
    encoded = base64.b64encode(text.encode()).decode()
    try:
        sys.stdout.write(f"\033]52;c;{encoded}\007")
        sys.stdout.flush()
        return True
    except OSError:
        return False


def ping_host(host: str, port: str = "22") -> int:
    """TCP-level reachability probe (works where ICMP ping is blocked)."""
    import socket
    host = host.split("@")[-1]
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            print(f"  ✓ {host}:{port} reachable")
            return 0
    except OSError as e:
        print(f"  ✗ {host}:{port} unreachable ({e})", file=sys.stderr)
        return 1


def handle_action(
    action: str,
    selected_name: str,
    session: Dict[str, Any],
    reg_path: Path,
    recent_file: Path,
    record: bool = True,
    dry_run: bool = False,
) -> int:
    """Dispatch a chosen action for a session; returns exit code."""
    if action == ACTION_COPY:
        cmd, _temp_key = build_ssh_command(
            unquote_display(selected_name), session, reg_path=reg_path,
            tmux=False,
        )
        if cmd and copy_to_clipboard(shlex.join(cmd)):
            print("Copied to clipboard (OSC52).")
            return 0
        print("Could not copy to clipboard.", file=sys.stderr)
        return 1
    if action == ACTION_PING:
        host = session.get("HostName", "").strip()
        port = session.get("PortNumber", "22")
        if not host:
            print("No hostname to ping.", file=sys.stderr)
            return 1
        return ping_host(host, port)
    return connect_to_session(
        selected_name, session, reg_path, recent_file,
        record=record, dry_run=dry_run,
    )


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

    # Loop: session pick -> action -> maybe back to session pick
    while True:
        picker = pick_session_fzf if args.fzf else None
        if picker is not None:
            selected_name = picker(usable, reg_path)
            if selected_name is None and shutil.which("fzf") is None:
                selected_name = pick_session_interactive(
                    usable, recent_sessions, group_by_tag=not args.flat
                )
        else:
            selected_name = pick_session_interactive(
                usable, recent_sessions, group_by_tag=not args.flat
            )
        if selected_name is None:
            print("\nSelection cancelled.")
            return 0

        action = pick_action(selected_name)
        if action is None or action == ACTION_BACK:
            continue  # back to the session list
        return handle_action(
            action, selected_name, usable[selected_name], reg_path, recent_file,
            record=not args.no_record, dry_run=args.dry_run,
        )


if __name__ == "__main__":
    sys.exit(main())