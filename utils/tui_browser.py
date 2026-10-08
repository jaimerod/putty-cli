"""Textual TUI session browser for putty-cli.

A persistent alternative to the simple-term_menu flow: filter box,
session list with tag grouping and recents metadata, live preview of
the ssh command for the highlighted session, and an action menu
(Connect via suspend() TTY handoff to tmux, Copy, Ping).
"""
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, ListItem, ListView, Static

from .display_util import make_display_pair, session_tag
from .recent_sessions_util import format_relative_time
from .ssh_command_util import build_ssh_command
from .titlecase_util import unquote_display

try:
    from simple_term_menu import TerminalMenu  # noqa: F401
    _HAS_MENU = True
except ImportError:
    _HAS_MENU = False


class SessionRow:
    """A selectable row in the session list."""

    def __init__(self, name: str, display: str, tag: Optional[str]):
        self.name = name
        self.display = display
        self.tag = tag

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"SessionRow({self.name!r})"


def build_rows(
    usable: Dict[str, Dict[str, Any]],
    recent_names: List[str],
    recent_entries: Optional[Dict[str, Dict[str, object]]],
    group_by_tag: bool = True,
) -> List[SessionRow]:
    """Rows in display order: recents (with metadata), then tag groups."""
    rows: List[SessionRow] = []
    if recent_names:
        for name in recent_names:
            if name in usable:
                rows.append(_row(name, usable[name], recent_entries))
        rows.append(SessionRow("__sep__", "─" * 60, None))

    remaining = [n for n in sorted(
        usable.keys(),
        key=lambda n: unquote_display(n).lower(),
    ) if n not in recent_names]

    if not group_by_tag:
        rows.extend(_row(n, usable[n], None) for n in remaining)
        return rows

    grouped: Dict[str, List[str]] = {}
    ungrouped: List[str] = []
    for name in remaining:
        tag = session_tag(name)
        if tag:
            grouped.setdefault(tag, []).append(name)
        else:
            ungrouped.append(name)

    for tag in sorted(grouped):
        rows.append(SessionRow(f"__hdr__:{tag}",
                               f"── {tag.upper()} " + "─" * 20, None))
        rows.extend(_row(n, usable[n], None) for n in grouped[tag])
    if grouped and ungrouped:
        rows.append(SessionRow("__hdr__:__other__", "── OTHER " + "─" * 20, None))
    rows.extend(_row(n, usable[n], None) for n in ungrouped)
    return rows


def _row(
    name: str, session: Dict[str, Any],
    recent_entries: Optional[Dict[str, Dict[str, object]]],
) -> SessionRow:
    _, display, _ = make_display_pair(name, session)
    if recent_entries and name in recent_entries:
        entry = recent_entries[name]
        ts = entry.get("ts")
        info = format_relative_time(
            int(ts) if isinstance(ts, (int, float)) else None)
        exit_code = entry.get("exit")
        if isinstance(exit_code, (int, float)) and int(exit_code) != 0:
            info += (" " if info else "") + "✗"
        if info:
            display = f"{display}  ({info})"
    return SessionRow(name, display, session_tag(name))


def preview_text(
    session: Dict[str, Any], reg_path: Optional[Path]
) -> str:
    """Side-effect-free ssh command preview for the highlighted session."""
    cmd, _ = build_ssh_command(
        "preview", session, reg_path=reg_path, tmux=False,
        quiet=True, convert_keys=False,
    )
    if not cmd:
        return "(no hostname)"
    lines = [
        "[b]ssh command[/b]",
        "[green]" + shlex.join(cmd) + "[/green]",
        "",
        "[b]options[/b]",
        f"port       {session.get('PortNumber', '22')}",
        f"user       {session.get('UserName', '-') or '-'}",
        f"jump       {session.get('ProxyHost', '-') or '-'}"
        if session.get("ProxyMethod", "0").strip() == "6" else
        "jump       -",
        f"key        {session.get('PublicKeyFile', '') or '-'}"
        if session.get("PublicKeyFile", "").strip() else "key        -",
        f"remote-cmd {session.get('RemoteCommand', '-') or '-'}"
        if session.get("RemoteCommand", "").strip() else "remote-cmd -",
        f"protocol   {session.get('Protocol', 'ssh')}",
    ]
    return "\n".join(lines)


class _SessionItem(ListItem):
    """ListItem bound to its SessionRow (Textual 8 dropped ListItem.data)."""

    def __init__(self, row: SessionRow, *widgets: Any) -> None:
        super().__init__(*widgets)
        self.row = row


class SessionBrowser(App[int]):
    """putty-cli session browser; returns 0 normally, 1 on error."""

    CSS = """
    #list-col { width: 2fr; }
    #preview-col { width: 1fr; border-left: solid $primary; padding: 0 1; }
    #preview { padding: 0 1; }
    #filter { dock: top; }
    ListView { height: 1fr; }
    Static#status { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "focus_filter", "Find"),
        Binding("enter", "connect", "Connect"),
        Binding("p", "ping", "Ping"),
        Binding("y", "copy", "Copy cmd"),
        Binding("l", "logs", "Logs"),
        Binding("escape", "blur_filter", "List"),
    ]

    def __init__(
        self,
        usable: Dict[str, Dict[str, Any]],
        recent_names: List[str],
        recent_entries: Dict[str, Dict[str, object]],
        reg_path: Optional[Path],
        group_by_tag: bool = True,
        connect_callback: Any = None,
    ):
        super().__init__()
        self.usable = usable
        self.recent_names = recent_names
        self.recent_entries = recent_entries
        self.reg_path = reg_path
        self.group_by_tag = group_by_tag
        self.connect_callback = connect_callback  # runs after suspend()
        self.selected_name: Optional[str] = None
        self.rows: List[SessionRow] = []
        self.filtered_rows: List[SessionRow] = []
        self.status_message = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="filter sessions…", id="filter")
        with Horizontal():
            with Vertical(id="list-col"):
                yield ListView(id="sessions")
            with Vertical(id="preview-col"):
                yield Static("select a session", id="preview", markup=True)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.rows = build_rows(
            self.usable, self.recent_names, self.recent_entries,
            self.group_by_tag)
        self._refresh_list()
        self._suspendable = True  # probed at action time (headless pilots)
        self.query_one("#sessions", ListView).focus()

    async def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    async def action_blur_filter(self) -> None:
        self.query_one("#sessions", ListView).focus()

    def on_input_changed(self, event: "Input.Changed") -> None:
        if event.input.id == "filter":
            self._refresh_list()
        # return focus to list on Enter via Input.Submitted
        event.stop()

    async def on_input_submitted(self, event: "Input.Submitted") -> None:
        if event.input.id == "filter":
            self.query_one("#sessions", ListView).focus()
            await self.action_connect()

    def _refresh_list(self) -> None:
        list_view = self.query_one("#sessions", ListView)
        filter_text = self.query_one("#filter", Input).value.lower().strip()
        self.filtered_rows = [
            r for r in self.rows
            if r.tag is None and r.name.startswith("__")  # headers/seps
            or filter_text in r.display.lower()
            or filter_text in unquote_display(r.name).lower()
        ]
        if filter_text:
            # filtering hides headers/separators entirely
            self.filtered_rows = [r for r in self.rows
                                  if not r.name.startswith("__")
                                  and (filter_text in r.display.lower()
                                       or filter_text in unquote_display(r.name).lower())]
        list_view.clear()
        for row in self.filtered_rows:
            selectable = not row.name.startswith("__")
            item = _SessionItem(row, Static(row.display, markup=False))
            if not selectable:
                item.add_class("header")
                item.disabled = True
            list_view.append(item)
        if self.filtered_rows:
            list_view.index = 0
        self._update_preview()
        self._update_status()

    def _update_status(self) -> None:
        n_total = len([r for r in self.rows if not r.name.startswith("__")])
        n_shown = len([r for r in self.filtered_rows if not r.name.startswith("__")])
        msg = f"{n_shown}/{n_total} sessions"
        if self.status_message:
            msg += f"  │  {self.status_message}"
        self.query_one("#status", Static).update(msg)

    def _current_row(self) -> Optional[SessionRow]:
        lv = self.query_one("#sessions", ListView)
        if lv.index is None or not self.filtered_rows:
            return None
        if 0 <= lv.index < len(self.filtered_rows):
            return self.filtered_rows[lv.index]
        return None

    async def on_list_view_selected(
        self, event: "ListView.Selected"
    ) -> None:
        if isinstance(event.item, _SessionItem):
            self.selected_name = event.item.row.name
            await self.action_connect()

    def on_list_view_highlight(self, event: ListView.Highlighted) -> None:
        self._update_preview()

    def _update_preview(self) -> None:
        row = self._current_row()
        preview = self.query_one("#preview", Static)
        if row is None or row.name.startswith("__"):
            preview.update("select a session")
            return
        preview.update(preview_text(self.usable[row.name], self.reg_path))

    async def action_quit(self) -> None:
        self.selected_name = None
        self.exit(0)

    def _selected_session(self) -> Optional[Tuple[str, Dict[str, Any]]]:
        row = self._current_row()
        if row is None or row.name.startswith("__"):
            return None
        return row.name, self.usable[row.name]

    async def action_connect(self) -> None:
        sel = self._selected_session()
        if sel is None:
            self.status_message = "no session highlighted"
            self._update_status()
            return
        name, _session = sel
        self.selected_name = name
        # suspend() releases the terminal so tmux can take over the TTY.
        # Headless pilots (tests) can't suspend: run the callback directly.
        try:
            with self.suspend():
                if self.connect_callback:
                    self.connect_callback(name)
        except SuspendNotSupported:
            if self.connect_callback:
                self.connect_callback(name)
        self.status_message = f"connected: {unquote_display(name)}"
        self._update_status()

    async def action_ping(self) -> None:
        sel = self._selected_session()
        if sel is None:
            return
        name, session = sel
        host = session.get("HostName", "").strip()
        port = session.get("PortNumber", "22")
        if not host:
            self.status_message = "no hostname to ping"
        else:
            from .tui_actions import run_ping
            ok, msg = run_ping(host, port)
            self.status_message = ("✓ " if ok else "✗ ") + msg
        self._update_status()

    async def action_copy(self) -> None:
        sel = self._selected_session()
        if sel is None:
            return
        name, session = sel
        from .tui_actions import copy_command
        cmd, _ = build_ssh_command(
            unquote_display(name), session, reg_path=self.reg_path,
            tmux=False, quiet=True,
        )
        if not cmd:
            self.status_message = "nothing to copy"
        elif copy_command(shlex.join(cmd)):
            self.status_message = "copied (OSC52/tmux buffer)"
        else:
            self.status_message = "copy failed"
        self._update_status()

    async def action_logs(self) -> None:
        from .tui_actions import LogsScreen
        self.push_screen(LogsScreen(self._logs_dir()))

    def _logs_dir(self) -> Path:
        from .logs_util import log_dir
        return log_dir()
