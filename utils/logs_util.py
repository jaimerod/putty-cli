import os
import subprocess  # nosec
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .ssh_command_util import safe_name

VIEWER_KEYS = {"n": "-N", "v": "-V"}


def log_dir(reg_path: Optional[Path] = None) -> Path:
    """Where session logs live - SAME resolution as _wrap_tmux:
    $PUTTY_LOG_DIR > the .reg file's directory > ~/.putty-cli/logs."""
    return Path(os.environ.get("PUTTY_LOG_DIR")
                or (reg_path.parent if reg_path is not None
                    else Path.home() / ".putty-cli" / "logs")).expanduser()


def list_logs(directory: Optional[Path] = None, limit: int = 30,
              session_name: Optional[str] = None,
              ) -> List[Tuple[Path, int, float]]:
    """Recent log files as (path, size_bytes, mtime), newest first.

    With session_name, only logs whose filename contains the sanitized
    session name (putty-cli's 'TIMESTAMP-NAME.log' convention). PuTTY
    exports URL-quoted names ('Foo%20Bar'), which the connect flow
    decodes before naming the log file - so match either form."""
    d = directory or log_dir()
    if not d.is_dir():
        return []
    variants = set()
    if session_name:
        from .titlecase_util import unquote_display
        unquoted = unquote_display(str(session_name))
        variants.add(safe_name(unquoted))
        variants.add(safe_name(str(session_name)))
    logs = [
        (p, p.stat().st_size, p.stat().st_mtime)
        for p in d.glob("*.log") if p.is_file()
        and (not variants
             or any(f"-{v}" in p.name or p.name == f"{v}.log"
                    for v in variants))
    ]
    logs.sort(key=lambda t: t[2], reverse=True)
    return logs[:limit]


def format_log_line(path: Path, size: int, mtime: float) -> str:
    age = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
    if size >= 1024 * 1024:
        size_str = f"{size // (1024 * 1024)}M"
    elif size >= 1024:
        size_str = f"{size // 1024}K"
    else:
        size_str = f"{size}B"
    return f"{path.name:<60} {age}  {size_str:>6}"


def pick_and_view_log(directory: Optional[Path] = None) -> Optional[int]:
    """Interactive log picker; opens the chosen log in $PAGER (or less).
    Returns the pager exit code, or None if there's nothing to show."""
    logs = list_logs(directory)
    if not logs:
        print(f"No session logs in {directory or log_dir()}.", file=sys.stderr)
        return None

    from simple_term_menu import TerminalMenu
    items = [format_log_line(*entry) for entry in logs]
    menu = TerminalMenu(
        items,
        title="\n Session logs  (Enter = view in pager / Esc = quit)\n",
        menu_cursor="➜ ",
        menu_cursor_style=("fg_green", "bold"),
        menu_highlight_style=("standout",),
        search_key="/",
    )
    idx = menu.show()
    if idx is None:
        return None

    path, _, _ = logs[idx]
    pager = os.environ.get("PAGER", "less")
    try:
        # user-configured $PAGER on our own log path, by design
        result = subprocess.run([pager, str(path)])  # nosec B603, B607
        return result.returncode
    except FileNotFoundError:
        # No pager installed: dump to stdout as fallback
        sys.stdout.write(path.read_text(errors="replace"))
        return 0
