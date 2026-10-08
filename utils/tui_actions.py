"""Action helpers used by the TUI (kept importable without a running app)."""
import os
import socket
import subprocess  # nosec
import sys
from pathlib import Path
from typing import List, Tuple

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, RichLog, Static


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


def _size_str(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size // (1024 * 1024)}M"
    if size >= 1024:
        return f"{size // 1024}K"
    return f"{size}B"


class LogViewerScreen(Screen):
    """Searchable view of ONE session log file.

    '/' focuses the search box; the log filters to matching lines live.
    n/N jump between matches; Esc returns.
    """

    BINDINGS = [
        ("slash", "focus_search", "Search"),
        ("escape", "dismiss", "Back"),
        ("q", "dismiss", "Back"),
        ("n", "next_match", "Next match"),
        ("N", "prev_match", "Prev match"),
    ]

    def __init__(self, log_path: Path, max_lines: int = 20000) -> None:
        super().__init__()
        self.log_path = log_path
        self.max_lines = max_lines
        self.lines: List[str] = []
        self.match_positions: List[int] = []
        self.match_index: int = -1

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(
            placeholder="/ search this log (n next match, N previous)",
            id="log-search",
        )
        with VerticalScroll(id="log-scroll"):
            yield RichLog(id="log-view", wrap=False, highlight=False,
                          max_lines=self.max_lines)
        yield Static("", id="log-status")
        yield Footer()

    def on_mount(self) -> None:
        self._load()
        self.query_one("#log-scroll", VerticalScroll).focus()

    def _load(self) -> None:
        try:
            text = self.log_path.read_text(errors="replace")
        except OSError as e:
            self.lines = [f"[red]Cannot read {self.log_path}: {e}[/red]"]
        else:
            self.lines = text.splitlines()
        self._write_lines(self.lines)
        self._update_status()

    def _write_lines(self, lines: List[str]) -> None:
        view = self.query_one("#log-view", RichLog)
        view.clear()
        for line in lines:
            view.write(line)

    def _update_status(self) -> None:
        shown = len(self.lines)
        msg = f"{self.log_path.name}  │  {shown} lines"
        if self.match_positions:
            msg += (f"  │  match {self.match_index + 1}/"
                    f"{len(self.match_positions)}")
        elif self.match_positions == [] and \
                self.query_one("#log-search", Input).value:
            msg += "  │  no matches"
        self.query_one("#log-status", Static).update(msg)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "log-search":
            self._apply_search()

    def _apply_search(self) -> None:
        term = self.query_one("#log-search", Input).value.strip()
        if not term:
            self.match_positions = []
            self._write_lines(self.lines)
        else:
            self.match_positions = [
                i for i, line in enumerate(self.lines) if term.lower() in line.lower()
            ]
            self._write_lines([
                self._mark(i, line) for i, line in enumerate(self.lines)
                if term.lower() in line.lower()
            ])
            self.match_index = 0 if self.match_positions else -1
        self._update_status()

    def _mark(self, index: int, line: str) -> str:
        return f"[b]{index + 1}[/b] {line}"

    async def action_focus_search(self) -> None:
        self.query_one("#log-search", Input).focus()

    async def action_next_match(self) -> None:
        self._jump_match(1)

    async def action_prev_match(self) -> None:
        self._jump_match(-1)

    def _jump_match(self, direction: int) -> None:
        """n/N: ensure matches exist for the current term, keep them stable
        across the jump (clearing the input re-runs _apply_search and
        wipes match state - so compute positions first, then clear)."""
        term = self.query_one("#log-search", Input).value.strip()
        if not term:
            return  # nothing to jump to
        if not self.match_positions:
            self.match_positions = [
                i for i, line in enumerate(self.lines)
                if term.lower() in line.lower()
            ]
            if not self.match_positions:
                self._update_status()
                return
        self.match_index = (self.match_index + direction) % len(self.match_positions)
        target = self.match_positions[self.match_index]
        # show context around the match (full window, filter cleared)
        self.query_one("#log-search", Input).value = ""
        self._write_lines(self._match_context(target))
        self._update_status()
        self._update_status()

    def _match_context(self, center: int, radius: int = 10) -> List[str]:
        start = max(0, center - radius)
        end = min(len(self.lines), center + radius + 1)
        return [
            f"[b]{i + 1}[/b] {self.lines[i]}" if i == center
            else f"  {i + 1} {self.lines[i]}"
            for i in range(start, end)
        ]

    async def action_dismiss(self, result: object = None) -> None:
        self.app.pop_screen()
