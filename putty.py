#!/usr/bin/env python3
"""
putty.py

Reads PuTTY .reg export → fuzzy searchable menu → ssh connect
Handles .ppk keys via puttygen (Linux version)
"""
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from utils.display_util import generate_menu_items
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
    print("  python3 -m venv venv", file=sys.stderr)
    print("  source venv/bin/activate", file=sys.stderr)
    print("  pip install -r recommendations.txt", file=sys.stderr)
    sys.exit(1)

# ─── Config ────────────────────────────────────────────────────────────────
REG_PATH = Path(
    "/mnt/c/Users/hiiam/OneDrive/Shared/portable/PuTTY/Data/settings/putty.reg"
).expanduser().resolve()
# ───────────────────────────────────────────────────────────────────────────

def main() -> None:
    """Main function to parse sessions, display menu, and handle connection."""
    recent_file = Path.home() / ".putty_recent_sessions"
    sessions: Dict[str, Dict[str, Any]] = parse_putty_reg(REG_PATH)

    if not sessions:
        print("No sessions found in .reg file.", file=sys.stderr)
        sys.exit(1)

    usable_sessions: Dict[str, Dict[str, Any]] = {
        k: v
        for k, v in sessions.items()
        if "HostName" in v and v["HostName"].strip() and not k.lower().startswith("xxx")
    }

    if not usable_sessions:
        print("No usable sessions found (missing HostName).", file=sys.stderr)
        sys.exit(1)

    recent_sessions: List[str] = load_recent_sessions(recent_file, usable_sessions)

    menu_items: List[str] = []
    display_to_name: Dict[str, str] = {}
    for display, name in generate_menu_items(usable_sessions, recent_sessions):
        menu_items.append(display)
        if name:
            display_to_name[display] = name

    print("\n" + "=" * 80)
    print(
        " PuTTY Sessions  (arrows / type to filter / Enter = connect / Esc = quit)"
    )
    print("=" * 80)

    terminal_menu = TerminalMenu(
        menu_items,
        title="Please select a session:",
        menu_cursor="➜ ",
        menu_cursor_style=("fg_green", "bold"),
        menu_highlight_style=("standout",),
        search_key="/",
        clear_screen=True,
        show_search_hint=True,
    )

    selected_index = terminal_menu.show()

    if selected_index is None:
        print("\nSelection cancelled.")
        return

    # Handle re-selection if a separator is chosen
    selected_display = menu_items[selected_index]
    while selected_display not in display_to_name:
        print("Invalid selection (separator). Please choose again.", file=sys.stderr)
        selected_index = terminal_menu.show()
        if selected_index is None:
            print("\nSelection cancelled.")
            return
        selected_display = menu_items[selected_index]

    selected_name = display_to_name[selected_display]
    session = usable_sessions[selected_name]

    print(f"\nSelected: {unquote_display(selected_name)}")

    cmd, temp_key = build_ssh_command(unquote_display(selected_name), session)

    if not cmd:
        print("Cannot build ssh command (missing hostname?).", file=sys.stderr)
        return

    print(f"Command: {cmd}\n")

    try:
        # Safely split command for subprocess and set shell=False
        cmd_list = shlex.split(cmd)
        result = subprocess.run(cmd_list, check=False)
        # Only update recents on successful exit (code 0)
        if result.returncode == 0:
            update_recent_sessions(recent_file, selected_name)
    except FileNotFoundError:
        print("Error: 'ssh' not found. Please install openssh-client.", file=sys.stderr)
    except ValueError:
        print(f"Error: Could not parse command: {cmd}", file=sys.stderr)
    finally:
        if temp_key and os.path.exists(temp_key):
            try:
                os.unlink(temp_key)
            except OSError as e:
                print(f"Warning: Could not remove temp key: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()