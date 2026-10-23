"""The interactive simple-term-menu flow (--no-tui, or the fallback
when textual is missing).

putty.py keeps CLI parsing, the non-interactive modes and the TUI
handoff; the pick -> act -> connect loop, the picker dialogs and the
connect orchestration live here so the entry point stays thin.
"""
import os
import shlex
import shutil
import subprocess  # nosec
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .clipboard_util import copy_command
from .display_util import generate_menu_items
from .net_util import run_ping
from .ppk_convert_util import ppk_cache_dir
from .putty_parse_util import file_stamp, parse_putty_reg_cached
from .recent_sessions_util import (
    load_recent_entries,
    load_recent_sessions,
    update_recent_sessions,
)
from .session_util import SessionMap, session_value, usable_sessions
from .ssh_command_util import build_ssh_command
from .titlecase_util import unquote_display

try:
    from simple_term_menu import TerminalMenu
except ImportError:
    print("Error: simple-term-menu not installed.", file=sys.stderr)
    print("Please use a venv and install dependencies:", file=sys.stderr)
    print("  python3 -m venv .venv", file=sys.stderr)
    print("  source .venv/bin/activate", file=sys.stderr)
    print("  pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

#: the CLI entry point re-invoked by fzf's --preview pane
ENTRY_SCRIPT = Path(__file__).resolve().parents[1] / "putty.py"


def build_and_connect(
    selected_name: str,
    session: Dict[str, Any],
    reg_path: Path,
    use_tmux: Optional[bool] = None,
) -> Tuple[Optional[List[str]], Optional[str], bool]:
    """Resolve tmux availability and build the launch argv.

    use_tmux: True/False forces the mode; None auto-detects (falls back
    to bare ssh when the tmux binary is missing). Returns
    (argv, temp_key, tmux_effectively_used)."""
    if use_tmux is None:
        use_tmux = shutil.which("tmux") is not None
    return (
        *build_ssh_command(unquote_display(selected_name), session,
                           reg_path=reg_path, tmux=use_tmux),
        use_tmux,
    )


def connect_to_session(
    selected_name: str,
    session: Dict[str, Any],
    reg_path: Path,
    recent_file: Path,
    record: bool = True,
    dry_run: bool = False,
    use_tmux: Optional[bool] = None,
) -> int:
    """Build and run (or just print) the ssh command for one session.

    Returns the process exit code (0 for dry-run)."""
    print(f"\nSelected: {unquote_display(selected_name)}")

    cmd, temp_key, tmux_used = build_and_connect(
        selected_name, session, reg_path, use_tmux=use_tmux
    )

    if not cmd:
        print("Cannot build ssh command (missing hostname?).", file=sys.stderr)
        return 1

    print(f"Command: {shlex.join(cmd)}\n")

    if dry_run:
        return 0

    try:
        # argv built internally by shlex.quote; no user shell text.
        # No phase-2 attach: the tmux launch itself is attached (blocks
        # until ssh exits), and pipe-pane runs in the same command chain.
        result = subprocess.run(cmd, check=False)  # nosec B603
        exit_code = result.returncode
    except FileNotFoundError:
        missing = "tmux" if tmux_used else "ssh"
        print(f"Error: '{missing}' not found.", file=sys.stderr)
        if tmux_used:
            print("Tip: install tmux, or rerun with --no-tmux to skip it.", file=sys.stderr)
        exit_code = 1
    else:
        # Record the session on any normal exit, including ssh failures
        # (255 = connection refused/timeout) so failed hosts stay handy
        # for a quick retry with the failure visible in the menu. tmux
        # launch failure (see FileNotFoundError above) is NOT recorded -
        # the session never actually opened.
        if record:
            update_recent_sessions(
                recent_file, selected_name, exit_code=exit_code
            )
    finally:
        # Cached keys live in the ppk cache dir and must survive; only
        # delete keys created outside it (cache-miss fallback temp files).
        if temp_key and os.path.exists(temp_key):
            cache_root = str(ppk_cache_dir())
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
        f"{shlex.quote(sys.executable)} {shlex.quote(str(ENTRY_SCRIPT))} "
        f"--connect {{}} --dry-run 2>/dev/null | head -40"
    )
    try:
        # fixed args; session names only in stdin, never in argv
        result = subprocess.run(  # nosec B607, B603
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
    recent_entries: Optional[Dict[str, Dict[str, object]]] = None,
) -> Optional[str]:
    """Show the menu; returns the selected session name or None on cancel."""
    menu_items: List[str] = []
    display_to_name: Dict[str, str] = {}
    for display, name in generate_menu_items(
        usable, recent_sessions, group_by_tag=group_by_tag,
        recent_entries=recent_entries,
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


def ping_host(host: str, port: str = "22") -> int:
    """CLI wrapper around the shared reachability probe; prints the
    result."""
    ok, msg = run_ping(host, port)
    if ok:
        print(f"  ✓ {msg}")
        return 0
    print(f"  ✗ {msg}", file=sys.stderr)
    return 1


def handle_action(
    action: str,
    selected_name: str,
    session: Dict[str, Any],
    reg_path: Path,
    recent_file: Path,
    record: bool = True,
    dry_run: bool = False,
    use_tmux: Optional[bool] = None,
) -> int:
    """Dispatch a chosen action for a session; returns exit code."""
    if action == ACTION_COPY:
        cmd, _temp_key = build_ssh_command(
            unquote_display(selected_name), session, reg_path=reg_path,
            tmux=False,
        )
        if cmd and copy_command(shlex.join(cmd)):
            print("Copied to clipboard (OSC52).")
            return 0
        print("Could not copy to clipboard.", file=sys.stderr)
        return 1
    if action == ACTION_PING:
        host = session_value(session, "HostName")
        port = session_value(session, "PortNumber", "22")
        if not host:
            print("No hostname to ping.", file=sys.stderr)
            return 1
        return ping_host(host, port)
    return connect_to_session(
        selected_name, session, reg_path, recent_file,
        record=record, dry_run=dry_run, use_tmux=use_tmux,
    )


def run_menu_flow(
    usable: SessionMap,
    recent_file: Path,
    reg_path: Path,
    *,
    include_archived: bool = False,
    group_by_tag: bool = True,
    use_fzf: bool = False,
    no_record: bool = False,
    no_tmux: bool = False,
    dry_run: bool = False,
) -> int:
    """The interactive loop: pick a session, pick an action, run it.

    Re-stats the .reg file before each pick and reloads it when it
    changed on disk (a long-lived menu can outlive a Windows-side
    re-export); a failed reload keeps showing the last good snapshot.
    Returns the process exit code."""
    recent_sessions = load_recent_sessions(recent_file, usable)
    recent_entries = load_recent_entries(recent_file)
    reg_stamp = file_stamp(reg_path)

    # Loop: session pick -> action -> maybe back to session pick
    while True:
        # Item 26: a long-lived menu can outlive an edit to the .reg file
        # (user re-exports on the Windows side). Re-stat before each pick
        # and reload when changed so new/renamed sessions appear.
        current_stamp = file_stamp(reg_path)
        if reg_stamp is not None and current_stamp != reg_stamp:
            try:
                reloaded = parse_putty_reg_cached(reg_path)
                usable = usable_sessions(reloaded,
                                        include_archived=include_archived)
                recent_sessions = load_recent_sessions(recent_file, usable)
                recent_entries = load_recent_entries(recent_file)
                reg_stamp = current_stamp
                print("↻ .reg file changed - session list reloaded.", file=sys.stderr)
            except (OSError, Exception):
                pass  # keep showing the last good snapshot
        if use_fzf:
            selected_name = pick_session_fzf(usable, reg_path)
            if selected_name is None and shutil.which("fzf") is None:
                selected_name = pick_session_interactive(
                    usable, recent_sessions, group_by_tag=group_by_tag,
                    recent_entries=recent_entries,
                )
        else:
            selected_name = pick_session_interactive(
                usable, recent_sessions, group_by_tag=group_by_tag,
                recent_entries=recent_entries,
            )
        if selected_name is None:
            print("\nSelection cancelled.")
            return 0

        action = pick_action(selected_name)
        if action is None or action == ACTION_BACK:
            continue  # back to the session list
        return handle_action(
            action, selected_name, usable[selected_name], reg_path,
            recent_file,
            record=not no_record, dry_run=dry_run,
            use_tmux=False if no_tmux else None,
        )