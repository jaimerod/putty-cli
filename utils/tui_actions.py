"""Action helpers used by the TUI (kept importable without a running app)."""
import os
import socket
import subprocess  # nosec
import sys
import time
from pathlib import Path
from typing import Tuple

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static


def run_ping(host: str, port: str = "22") -> Tuple[bool, str]:
    """TCP-level reachability probe. Returns (ok, message)."""
    host = host.split("@")[-1]
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            return True, f"{host}:{port} reachable"
    except OSError as e:
        return False, f"{host}:{port} unreachable ({e})"


def copy_command(text: str) -> bool:
    """Copy via tmux buffer (inside tmux) or OSC52 escape sequence."""
    if os.environ.get("TMUX"):
        try:
            # fixed argv, 'tmux' from PATH; text goes via stdin buffer
            result = subprocess.run(  # nosec B603, B607
                ["tmux", "load-buffer", "-w", "-"],
                input=text.encode(), capture_output=True, timeout=5)
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    # OSC52: works in terminals that support it, even over ssh chains
    import base64
    try:
        encoded = base64.b64encode(text.encode()).decode()
        sys.stdout.write(f"\033]52;c;{encoded}\007")
        sys.stdout.flush()
        return True
    except OSError:
        return False


class LogsScreen(Screen):
    """In-app session log browser (replaces $PAGER flow inside the TUI)."""

    BINDINGS = [
        ("escape", "dismiss", "Back"),
        ("q", "dismiss", "Back"),
    ]

    def __init__(self, logs_dir: Path) -> None:
        super().__init__()
        self.logs_dir = logs_dir

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll():
            yield Static(self._list_text(), id="loglist", markup=True)
        yield Footer()

    def _list_text(self) -> str:
        from .logs_util import list_logs
        logs = list_logs(self.logs_dir, limit=200)
        if not logs:
            return f"[yellow]No session logs in {self.logs_dir}[/yellow]"
        lines = [f"[b]{len(logs)} session logs in {self.logs_dir}[/b]", ""]
        for path, size, mtime in logs:
            age = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
            if size >= 1024 * 1024:
                size_str = f"{size // (1024 * 1024)}M"
            elif size >= 1024:
                size_str = f"{size // 1024}K"
            else:
                size_str = f"{size}B"
            lines.append(f"{path.name:<60} {age}  {size_str:>6}")
        return "\n".join(lines)

    async def action_dismiss(self, result: object = None) -> None:
        self.app.pop_screen()
