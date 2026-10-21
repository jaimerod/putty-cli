"""Textual TUI session browser for putty-cli.

A persistent alternative to the simple-term_menu flow: filter box, a
left column with two stacked panes (recent connections above, all
connections below, FAR panel style), live preview of the ssh command
for the highlighted session, and an action menu (Connect via suspend()
TTY handoff to tmux, Copy, Ping).
"""
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from textual import events
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, ListItem, ListView, Static

from .display_util import session_tag
from .far_style import (
    FAR_KEYBAR_CSS,
    FAR_SHADOW_CSS,
    FAR_WINDOW_CSS,
    FARKeyBar,
    FarShadow,
    FarWindow,
    KeyBarClicked,
    install_far_theme,
)
from .putty_reg_write_util import (
    RegEditError,
    edit_session_values,
    reg_file_stamp,
)
from .recent_sessions_util import format_relative_time
from .ssh_command_util import build_ssh_command
from .titlecase_util import smart_title, unquote_display

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


def build_recent_rows(
    usable: Dict[str, Dict[str, Any]],
    recent_names: List[str],
    recent_entries: Optional[Dict[str, Dict[str, object]]],
) -> List[SessionRow]:
    """RECENT pane rows: recently connected sessions, newest first,
    carrying the relative-time / failure metadata."""
    return [
        _row(name, usable[name], recent_entries)
        for name in recent_names if name in usable
    ]


def build_all_rows(
    usable: Dict[str, Dict[str, Any]],
    group_by_tag: bool = True,
) -> List[SessionRow]:
    """ALL pane rows: every session (recents included), tag-grouped."""
    names = sorted(usable.keys(), key=lambda n: unquote_display(n).lower())
    if not group_by_tag:
        return [_row(n, usable[n], None) for n in names]

    grouped: Dict[str, List[str]] = {}
    ungrouped: List[str] = []
    for name in names:
        tag = session_tag(name)
        if tag:
            grouped.setdefault(tag, []).append(name)
        else:
            ungrouped.append(name)

    rows: List[SessionRow] = []
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
    # no make_display_pair here: the "→ user@host" hint lives in the
    # CONNECTION DETAIL pane now; rows stay FAR-panel-terse and single-line
    display = smart_title(unquote_display(name))
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
        "[b]SSH Command[/b]",
        "[green]" + shlex.join(cmd) + "[/green]",
        "",
        "[b]Options[/b]",
        f"Port        {session.get('PortNumber', '22')}",
        f"User        {session.get('UserName', '-') or '-'}",
        f"Jump Host   {session.get('ProxyHost', '-') or '-'}"
        if session.get("ProxyMethod", "0").strip() == "6" else
        "Jump Host   -",
        f"Key         {session.get('PublicKeyFile', '') or '-'}"
        if session.get("PublicKeyFile", "").strip() else "Key         -",
        f"Remote Cmd  {session.get('RemoteCommand', '-') or '-'}"
        if session.get("RemoteCommand", "").strip() else "Remote Cmd  -",
        f"Protocol    {session.get('Protocol', 'ssh')}",
    ]
    return "\n".join(lines)


class _SessionItem(ListItem):
    """ListItem bound to its SessionRow (Textual 8 dropped ListItem.data).

    ListView.Selected (mouse click on a row) is select-only: it only
    moves the highlight. Connecting is done with Enter or from the
    right-click menu."""

    def __init__(self, row: SessionRow, browser: "SessionBrowser",
                 *widgets: Any) -> None:
        super().__init__(*widgets)
        self.row = row
        self.browser = browser


class _PaneList(ListView):
    """One of the two session panes.

    Tab jumps straight to the other pane (FAR panel style) instead of
    walking the focus chain; / reaches the filter."""

    BINDINGS = [Binding("tab", "app.next_pane", "Pane", show=False)]


async def app_push_screen(
    app: "SessionBrowser", modal: Any, session_name: str
) -> None:
    """Push a modal and dispatch its dismiss result (Textual 8: awaiting
    push_screen() waits for MOUNT, not the result - callback form is the
    only way to receive the dismiss value without a worker)."""
    # The modal is transparent: the app's footer shows through, so swap
    # it to the keys that work while the menu is open, restore on close.
    app._set_keybar(SessionBrowser.KEYBAR_MENU)

    def on_result(action: Optional[str]) -> None:
        app._set_keybar(SessionBrowser.KEYBAR_LIST)
        app.call_later(app._run_chosen_action, action, session_name)

    app.push_screen(modal, callback=on_result)


class SessionBrowser(App[int]):
    """putty-cli session browser; returns 0 normally, 1 on error."""

    CSS = """
    /* FAR palette: blue bg, silver text, cyan selection */
    .browser-bg { background: $background; }
    ListView { height: 1fr; background: $background; color: $foreground; }
    ListView > ListItem.--highlight { background: $primary; color: #000000; }
    #filter { dock: top; background: $background; color: $foreground;
             border: solid #AAAAAA; margin: 0 1 1 1; }
    #title-bar { dock: top; height: 1; color: $secondary;
                 background: $background; padding: 0 1; }
    #list-col { width: 2fr; }
    #recent-win { height: 40%; }
    #all-win { height: 1fr; }
    #preview-col {
        width: 1fr;
        padding: 0 1;
        color: $foreground;
        background: $background;
    }
    #preview { padding: 0 1; color: $foreground; }
    Static#status { dock: bottom; height: 1; color: $primary;
                    background: $background; padding: 0 1; }
    FARKeyBar { dock: bottom; }
    FARKeyBar#ctx-keybar { dock: bottom; }
    """ + FAR_SHADOW_CSS + FAR_WINDOW_CSS + FAR_KEYBAR_CSS

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "focus_filter", "Find"),
        # priority: must pre-empt the focused ListView's own Enter
        # (select_cursor), else a second 'Selected' fires and double-fires
        # (check_action disables it while the action menu / log viewer is
        # on top, so their own Enter keys still work)
        Binding("enter", "connect", "Connect", priority=True),
        Binding("p", "ping", "Ping"),
        Binding("y", "copy", "Copy Command"),
        Binding("l", "logs", "View Session Log"),
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
        self._last_cursor: tuple[int, int] = (0, 0)
        # two panes: #recents (top) and #sessions = the ALL pane (bottom)
        self.recent_rows: List[SessionRow] = []
        self.rows: List[SessionRow] = []
        self.filtered_recent_rows: List[SessionRow] = []
        self.filtered_rows: List[SessionRow] = []
        self._active_list_id = "sessions"
        self.status_message = ""
        self._edit_stamp: Optional[str] = None

    KEYBAR = [
        ("F1", "help"), ("F3", "view log"), ("F4", "edit"),
        ("F5", "copy cmd"), ("F6", "ping"), ("F7", "filter"),
        ("F10", "quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(
            " PuTTY Sessions  ▒  Enter connect · Tab panes · right-click "
            "menu · / filter",
            id="title-bar", markup=False,
        )
        yield Input(placeholder="/filter…", id="filter")
        with Horizontal():
            # far-shadow mounts BEFORE its window -> renders underneath
            with Vertical(id="list-col"):
                yield FarShadow()
                with FarWindow(id="recent-win", title="RECENT CONNECTIONS"):
                    yield _PaneList(id="recents")
                with FarWindow(id="all-win", title="ALL CONNECTIONS"):
                    yield _PaneList(id="sessions")
            with Vertical(id="preview-col"):
                yield FarShadow()
                with FarWindow(id="preview-win",
                               title="CONNECTION DETAIL"):
                    yield Static("select a session", id="preview",
                                 markup=True)
        yield Static("", id="status")
        yield FARKeyBar(
            [("Enter", "Connect"), ("RMB", "Menu"),
             ("/", "Find"), ("Tab", "Pane"),
             ("p", "Ping"), ("y", "Copy"), ("l", "Log"), ("q", "Quit")],
            id="keybar",
        )

    KEYBAR_LIST = [
        ("Enter", "Connect", "connect"), ("RMB", "Menu", None),
        ("/", "Find", "focus_filter"), ("Tab", "Pane", "next_pane"),
        ("p", "Ping", "ping"), ("y", "Copy", "copy"), ("l", "Log", "logs"),
        ("q", "Quit", "quit"),
    ]
    KEYBAR_FILTER = [
        ("Enter", "Pick+Connect", None), ("Esc", "Back to list", "blur_filter"),
        ("q", "Quit", "quit"),
    ]
    KEYBAR_MENU = [
        ("↑↓", "Navigate", None), ("Enter", "Select", None),
        ("Esc", "Close", None),
    ]
    KEYBAR_EDIT = [
        ("Tab", "Next", None), ("Enter", "OK", None),
        ("Esc", "Cancel", None),
    ]
    KEYBAR_LOG = [
        ("/", "Search", None), ("n", "Next", None), ("N", "Prev", None),
        ("Esc", "Back", None),
    ]

    def _set_keybar(self, entries: List[Tuple]) -> None:
        try:
            self.query_one("#keybar", FARKeyBar).set_entries(entries)
        except Exception:  # nosec B110 - not mounted yet
            pass

    def on_mount(self) -> None:
        install_far_theme(self)
        self.screen.add_class("browser-bg")
        self.recent_rows = build_recent_rows(
            self.usable, self.recent_names, self.recent_entries)
        self.rows = build_all_rows(self.usable, self.group_by_tag)
        self._refresh_list()
        self._suspendable = True  # probed at action time (headless pilots)
        # FAR opens on the active panel: recent connections if there are
        # any, else the all-connections pane
        self.query_one(
            "#recents" if self.recent_rows else "#sessions", ListView
        ).focus()

    async def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()
        self._set_keybar(self.KEYBAR_FILTER)

    def action_next_pane(self) -> None:
        """Tab: jump between the recent pane and the all pane."""
        other_id = ("sessions" if self._active_list_id == "recents"
                    else "recents")
        try:
            self.query_one(f"#{other_id}", ListView).focus()
        except Exception:  # nosec B110 - pane not mounted
            pass

    async def action_blur_filter(self) -> None:
        self.query_one(f"#{self._active_list_id}", ListView).focus()
        self._set_keybar(self.KEYBAR_LIST)

    async def on_key_bar_clicked(
        self, event: "KeyBarClicked"
    ) -> None:
        """Clickable FAR key bar: dispatch the slot's mapped action."""
        act = event.action
        if act and hasattr(self, f"action_{act}"):
            await self.run_action(act)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """Context-sensitive footer: filter vs session pane hotkeys."""
        widget = event.widget
        if isinstance(widget, ListView):
            self._active_list_id = widget.id or "sessions"
            self._set_keybar(self.KEYBAR_LIST)
        elif isinstance(widget, Input) and widget.id == "filter":
            self._set_keybar(self.KEYBAR_FILTER)

    def on_input_changed(self, event: "Input.Changed") -> None:
        if event.input.id == "filter":
            self._refresh_list()
        # return focus to list on Enter via Input.Submitted
        event.stop()

    async def on_input_submitted(self, event: "Input.Submitted") -> None:
        if event.input.id == "filter":
            self.query_one(f"#{self._active_list_id}", ListView).focus()
            await self.action_connect()

    def _filter_rows(self, rows: List[SessionRow],
                     text: str) -> List[SessionRow]:
        if not text:
            return list(rows)
        # filtering hides headers/separators entirely
        def matches(row: SessionRow) -> bool:
            if text in row.display.lower() \
                    or text in unquote_display(row.name).lower():
                return True
            # the row shows just the name now; host/user/port stay
            # searchable so "example.com" or "2222" still finds sessions
            session = self.usable.get(row.name, {})
            blob = " ".join(
                str(session.get(k, "") or "")
                for k in ("HostName", "UserName", "PortNumber")).lower()
            return text in blob
        return [r for r in rows
                if not r.name.startswith("__") and matches(r)]

    def _fill_list(self, list_view: ListView, rows: List[SessionRow]) -> None:
        list_view.clear()
        for row in rows:
            selectable = not row.name.startswith("__")
            item = _SessionItem(row, self, Static(row.display, markup=False))
            if not selectable:
                item.add_class("header")
                item.disabled = True
            list_view.append(item)
        if rows:
            # Land on the first selectable row: index 0 may be a disabled
            # header/separator, and ListView won't highlight past it.
            first_selectable = next(
                (i for i, r in enumerate(rows)
                 if not r.name.startswith("__")),
                None,
            )
            if first_selectable is None:
                list_view.index = 0  # only headers visible (e.g. stale filter)
            elif list_view.index is None or \
                    not 0 <= list_view.index < len(rows) or \
                    rows[list_view.index].name.startswith("__"):
                list_view.index = first_selectable

    def _refresh_list(self) -> None:
        filter_text = self.query_one("#filter", Input).value.lower().strip()
        self.filtered_recent_rows = self._filter_rows(
            self.recent_rows, filter_text)
        self.filtered_rows = self._filter_rows(self.rows, filter_text)
        self._fill_list(self.query_one("#recents", ListView),
                        self.filtered_recent_rows)
        self._fill_list(self.query_one("#sessions", ListView),
                        self.filtered_rows)
        self._update_preview()
        self._update_status()

    def _update_status(self) -> None:
        n_total = len([r for r in self.rows if not r.name.startswith("__")])
        n_shown = len([r for r in self.filtered_rows if not r.name.startswith("__")])
        msg = f"{n_shown}/{n_total} sessions"
        if self.status_message:
            msg += f"  │  {self.status_message}"
        self.query_one("#status", Static).update(msg)

    def _pane_rows(self, list_id: str) -> List[SessionRow]:
        """Filtered rows of a pane, by widget id."""
        return (self.filtered_recent_rows if list_id == "recents"
                else self.filtered_rows)

    def _current_row(self) -> Optional[SessionRow]:
        try:
            lv = self.query_one(f"#{self._active_list_id}", ListView)
        except Exception:  # noqa: BLE001 - not mounted yet
            return None
        rows = self._pane_rows(self._active_list_id)
        if lv.index is None or not rows:
            return None
        if 0 <= lv.index < len(rows):
            return rows[lv.index]
        return None

    def _pane_of_item(self, item: Any) -> Optional[ListView]:
        """The ListView pane containing the given item (walk ancestors)."""
        parent: Any = item.parent
        while parent is not None and not isinstance(parent, ListView):
            parent = parent.parent
        return parent

    async def on_list_view_selected(
        self, event: "ListView.Selected"
    ) -> None:
        """Selected from ListView = mouse click on a row (Enter is
        captured by the app-level priority binding): select-only."""
        if isinstance(event.item, _SessionItem):
            self.select_session_item(event.item)

    def select_session_item(self, item: "_SessionItem") -> None:
        """Left click on a row: highlight it (the preview follows)."""
        pane = self._pane_of_item(item)
        if pane is not None:
            try:
                pane.index = pane.children.index(item)
            except ValueError:
                pass
            self._active_list_id = pane.id or "sessions"
        self._update_preview()

    async def on_click(self, event: "events.Click") -> None:
        """Right-click (button 3) on a session row opens the action menu.

        Left clicks are handled by the ListView itself: they only move
        the highlight (the preview follows). Connecting is done with
        Enter or from the action menu."""
        if event.button != 3:
            return
        # find the session row under the cursor (walk up the widget tree)
        widget: Any = event.widget
        while widget is not None and not isinstance(widget, _SessionItem):
            widget = widget.parent
        if widget is None or not isinstance(widget, _SessionItem):
            return
        name = widget.row.name
        if name.startswith("__"):
            return
        event.stop()
        self._last_cursor = (event.screen_x, event.screen_y)
        self.selected_name = name
        pane = self._pane_of_item(widget)
        if pane is not None:
            try:
                pane.index = pane.children.index(widget)
            except ValueError:
                pass
            self._active_list_id = pane.id or "sessions"
        self._update_preview()
        await self.run_action_with_modal(name)

    async def run_action_with_modal(self, session_name: str) -> None:
        from .tui_actions import SessionActionMenu
        underlying = self.screen_stack[-1] if self.screen_stack else None
        modal = SessionActionMenu(
            unquote_display(session_name), self._last_cursor,
            underlying=underlying)
        await app_push_screen(self, modal, session_name)

    async def _run_chosen_action(self, action: Optional[str], name: str) -> None:
        if action == "connect":
            self.selected_name = name
            await self.action_connect()
        elif action == "ping":
            self.selected_name = name
            await self.action_ping()
        elif action == "copy":
            self.selected_name = name
            await self.action_copy()
        elif action == "logs":
            self.selected_name = name
            await self.action_logs()
        elif action == "edit":
            await self._open_edit_modal(name)
        # None = cancelled: nothing to do

    async def _open_edit_modal(self, session_name: str) -> None:
        """FAR-style edit dialog over the browser; saves via the surgical
        .reg writer (never regenerating untouched bytes)."""
        from .tui_actions import EditSessionModal
        session = self.usable.get(session_name)
        if session is None:
            return
        if self.reg_path is None:
            self.status_message = "no .reg file to edit"
            self._update_status()
            return
        self._edit_stamp = reg_file_stamp(self.reg_path)
        self._set_keybar(self.KEYBAR_EDIT)
        modal = EditSessionModal(session_name, session)

        def on_result(changes: Optional[Dict[str, str]]) -> None:
            self._set_keybar(self.KEYBAR_LIST)
            self.call_later(self._edit_saved, changes, session_name)

        self.push_screen(modal, callback=on_result)

    async def _edit_saved(
        self, changes: Optional[Dict[str, str]], session_name: str,
    ) -> None:
        if changes is None:
            self.status_message = "edit cancelled"
        elif not changes:
            self.status_message = "saved (nothing to change)"
        else:
            reg_path = self.reg_path
            if reg_path is None:  # guarded when the dialog opened
                self.status_message = "no .reg file to edit"
            else:
                try:
                    result = edit_session_values(
                        reg_path, session_name, changes,
                        expect_stamp=self._edit_stamp)
                except RegEditError as e:
                    self.status_message = f"✗ edit not saved: {e}"
                else:
                    self.usable[session_name] = result.session_values
                    self._rebuild_rows()
                    backup = result.backup_path.name if result.backup_path \
                        else "no backup"
                    self.status_message = f"saved ✓ (backup: {backup})"
        self._update_status()

    def _rebuild_rows(self) -> None:
        self.recent_rows = build_recent_rows(
            self.usable, self.recent_names, self.recent_entries)
        self.rows = build_all_rows(self.usable, self.group_by_tag)
        self._refresh_list()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._update_preview()

    def _update_preview(self) -> None:
        row = self._current_row()
        preview = self.query_one("#preview", Static)
        if row is None or row.name.startswith("__"):
            preview.update("select a session")
            return
        preview.update(preview_text(self.usable[row.name], self.reg_path))

    def check_action(self, action: str, parameters: tuple) -> bool:
        """Disable the priority Enter binding while a modal/log screen is
        active so the modal's own widgets and keys keep working."""
        if action == "connect":
            from .tui_actions import EditSessionModal, LogViewerScreen, SessionActionMenu
            return type(self.screen) not in (
                SessionActionMenu, LogViewerScreen, EditSessionModal)
        return True

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
        """Newest log for the highlighted session, in the searchable viewer."""
        sel = self._selected_session()
        if sel is None:
            self.status_message = "no session highlighted"
            self._update_status()
            return
        from .logs_util import list_logs
        logs = list_logs(self._logs_dir(), limit=10,
                         session_name=sel[0])
        if not logs:
            self.status_message = f"no logs yet for {unquote_display(sel[0])}"
            self._update_status()
            return
        from .tui_actions import LogViewerScreen
        self._set_keybar(self.KEYBAR_LOG)
        self.push_screen(
            LogViewerScreen(logs[0][0]),
            callback=lambda _r: self._set_keybar(self.KEYBAR_LIST),
        )

    def _logs_dir(self) -> Path:
        from .logs_util import log_dir
        return log_dir()
