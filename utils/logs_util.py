import os
import subprocess  # nosec
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

VIEWER_KEYS = {"n": "-N", "v": "-V"}


def log_dir() -> Path:
    """Where session logs live (same resolution as _wrap_tmux)."""
    return Path(os.environ.get("PUTTY_LOG_DIR")
                or (Path.home() / ".putty-cli" / "logs")).expanduser()


def list_logs(directory: Optional[Path] = None, limit: int = 30) -> List[Tuple[Path, int, float]]:
    """Recent log files as (path, size_bytes, mtime), newest first."""
    d = directory or log_dir()
    if not d.is_dir():
        return []
    logs = [
        (p, p.stat().st_size, p.stat().st_mtime)
        for p in d.glob("*.log") if p.is_file()
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
        # user-configured $PAGER on our own log path, by design
        result = subprocess.run([pager, str(path)])  # nosec B603, B607
  # nosec B603, B607

        return result.returncode
    except FileNotFoundError:
        # No pager installed: dump to stdout as fallback
        sys.stdout.write(path.read_text(errors="replace"))
        return 0
