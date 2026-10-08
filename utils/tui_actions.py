"""Action helpers used by the TUI (kept importable without a running app)."""
import os
import socket
import subprocess  # nosec
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

from textual import events, on
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, Label, RichLog, Static

from .far_style import FARKeyBar


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


class SessionActionMenu(ModalScreen[Optional[str]]):
    """Cursor-positioned context menu (popup) for one session.

    A small option box opens where the cursor is; arrows navigate,
    Enter executes, Esc or clicking outside closes. Dismiss result:
    'connect' | 'ping' | 'copy' | 'logs' | None.
    """

    DEFAULT_CSS = """
    SessionActionMenu { background: transparent !important; }

    #ctx-menu {
        layout: vertical;
        /* FAR menu: blue bg, silver single-line frame, DOS drop shadow
           via Textual box-shadow (2 right, 1 down, translucent black) */
        background: #0000AA;
        border: solid #AAAAAA;
        width: auto;   /* sized to the widest child */
        height: auto;
        padding: 0;
        layer: window;
        box-sizing: border-box;
    }

    /* children must not claim free space */
    #ctx-menu VerticalScroll {
        width: auto;
        height: auto;
        max-height: 16;
    }

    #ctx-title {
        color: #FFFF55;   /* FAR: menu captions are yellow */
        background: #0000AA;
        padding: 0 2;
        height: 1;
        width: auto;
    }

    /* options: silver text on blue, full-row width */
    .ctx-option {
        background: #0000AA;
        color: #AAAAAA;
        padding: 0 2;
        height: 1;
        width: auto;
    }

    /* selected: FAR cursor bar - cyan with black text */
    .ctx-option.-selected {
        background: #00AAAA;
        color: #000000;
        text-style: bold;
    }

    /* DOS drop shadow for the menu box: absolute black 50% offset 2,1 */
    /* starts 0-sized; _sync_shadow() sizes it to the box + offset */
    #ctx-shadow {
        position: absolute;
        width: 0;
        height: 0;
        background: #000000 50%;
        layer: shadow;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Close"),
    ]

    ITEMS = [
        ("Connect", "connect"),
        ("Ping", "ping"),
        ("Copy ssh command", "copy"),
        ("View logs", "logs"),
    ]

    def __init__(self, session_name: str, cursor: tuple[int, int]) -> None:
        super().__init__()
        self.session_name = session_name
        self.cursor = cursor
        self._index = 0

    def _build_option(self, label: str) -> Static:
        # pad all rows to the longest label so the highlight bar spans
        # the full menu width evenly
        longest = max(len(item_label) for item_label, _ in self.ITEMS)
        return Static(
            label + " " * (longest - len(label)),
            classes="ctx-option", markup=False,
        )

    def compose(self) -> ComposeResult:
        from textual.containers import VerticalScroll
        yield Static("", id="ctx-shadow", markup=False)
        with Vertical(id="ctx-menu"):
            yield Label(self.session_name, id="ctx-title")
            with VerticalScroll(id="ctx-items"):
                for label, _action in self.ITEMS:
                    yield self._build_option(label)

    def on_mount(self) -> None:
        menu = self.query_one("#ctx-menu", Vertical)
        # box fits its longest option; keep it on screen
        box_w = max(len(label) for label, _ in self.ITEMS) + 4  # padding+border
        box_h = len(self.ITEMS) + 1  # + title row
        size = self.size
        x = min(self.cursor[0] - 1, max(0, size.width - box_w - 1))
        y = min(self.cursor[1], max(0, size.height - box_h - 2))
        menu.styles.offset = (x, y)
        self._render_items()
        # box.region is 0 until the first layout pass; sync after refresh
        self.call_after_refresh(self._sync_shadow)

    def _sync_shadow(self) -> None:
        """Shadow matches the menu box exactly, offset +2/+1 (DOS)."""
        try:
            box = self.query_one("#ctx-menu")
            shadow = self.query_one("#ctx-shadow")
        except Exception:  # noqa: BLE001 - widgets not mounted yet
            return
        if box.region.width:
            shadow.styles.width = box.region.width
            shadow.styles.height = box.region.height
            shadow.styles.offset = (box.region.x + 2, box.region.y + 1)

    def _render_items(self) -> None:
        """(Re)apply the -selected class on exactly one option row."""
        options = list(self.query(".ctx-option"))
        for i, opt in enumerate(options):
            if i == self._index:
                opt.add_class("-selected")
            else:
                opt.remove_class("-selected")

    def _highlight(self, index: int) -> None:
        self._index = max(0, min(index, len(self.ITEMS) - 1))
        self._render_items()

    def on_key(self, event: events.Key) -> None:
        if event.key == "down":
            self._highlight(self._index + 1)
            event.stop()
            event.prevent_default()
        elif event.key == "up":
            self._highlight(self._index - 1)
            event.stop()
            event.prevent_default()
        elif event.key == "enter":
            self.dismiss(self.ITEMS[self._index][1])
            event.stop()
            event.prevent_default()

    def _option_index(self, widget: Any) -> Optional[int]:
        """Index of the .ctx-option row containing `widget` (or None if
        the widget is inside the box but not on an option row)."""
        while widget is not None:
            if isinstance(widget, Static) and "ctx-option" in widget.classes:
                return list(self.query(".ctx-option")).index(widget)
            widget = widget.parent
        return None

    def _inside_menu(self, event: events.MouseEvent) -> bool:
        menu = self.query_one("#ctx-menu")
        widget: Any = event.widget
        while widget is not None:
            if widget is menu:
                return True
            widget = widget.parent
        return False

    @on(events.MouseMove)
    def _on_hover(self, event: events.MouseMove) -> None:
        """Hover over an option moves the highlight to it."""
        if not self._inside_menu(event):
            return
        idx = self._option_index(event.widget)
        if idx is not None and idx != self._index:
            self._highlight(idx)
        event.stop()

    def on_click(self, event: events.Click) -> None:
        """Left click on an option executes it; click outside closes."""
        if not self._inside_menu(event):
            self.dismiss(None)
            event.stop()
            return
        idx = self._option_index(event.widget)
        if idx is not None:
            self._highlight(idx)
            self.dismiss(self.ITEMS[idx][1])
        event.stop()

    async def action_cancel(self) -> None:
        self.dismiss(None)


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

    DEFAULT_CSS = """
    LogViewerScreen { background: #0000AA; }
    #log-search {
        dock: top;
        background: #0000AA;
        color: #FFFF55;
        border: hidden;
        height: 1;
        padding: 0 1;
    }
    #log-scroll {
        border: solid #AAAAAA;
        background: #0000AA;
        margin: 1 2 1 2;
    }
    #log-status {
        dock: bottom;
        height: 1;
        background: #0000AA;
        color: #00AAAA;
        padding: 0 2;
    }
    RichLog { background: #0000AA; color: #AAAAAA; }
    """

    def compose(self) -> ComposeResult:
        yield Static(f" {self.log_path.name}", id="log-title")
        yield Input(
            placeholder="/ search this log (n next match, N previous)",
            id="log-search",
        )
        with VerticalScroll(id="log-scroll"):
            yield RichLog(id="log-view", wrap=False, highlight=False,
                          max_lines=self.max_lines)
        yield Static("", id="log-status")
        yield FARKeyBar([
            ("/", "Search"), ("n", "Next"), ("N", "Prev"),
            ("Esc", "Back"),
        ])

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
