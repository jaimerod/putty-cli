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
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static

from .display_util import session_tag
from .far_dialog import FarValueField
from .far_style import (
    ACTIVE_PALETTE,
    FAR_KEYBAR_CSS,
    FAR_SHADOW_CSS,
    FAR_WINDOW_CSS,
    FARKeyBar,
    FarShadow,
    FarWindow,
    KeyBarOwner,
    install_far_theme,
)
from .putty_parse_util import file_stamp, parse_putty_reg_cached
from .putty_reg_write_util import (
    RegEditError,
    delete_session,
    edit_session_values,
    reg_file_stamp,
)
from .recent_sessions_util import (
    recent_info_suffix,
    remove_entry,
    rename_entry,
)
from .session_util import (
    SESSION_FIELDS,
    jump_host_of,
    session_value,
    usable_sessions,
)
from .ssh_command_util import build_ssh_command
from .titlecase_util import smart_title, unquote_display

try:
    from simple_term_menu import TerminalMenu  # noqa: F401
    _HAS_MENU = True
except ImportError:
    _HAS_MENU = False


class SessionRow:
    """A selectable row in the session list."""

    def __init__(self, name: str, display: str):
        self.name = name
        self.display = display

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
                               f"── {tag.upper()} " + "─" * 20))
        rows.extend(_row(n, usable[n], None) for n in grouped[tag])
    if grouped and ungrouped:
        rows.append(SessionRow("__hdr__:__other__", "── OTHER " + "─" * 20))
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
        info = recent_info_suffix(recent_entries[name])
        if info:
            display = f"{display}  ({info})"
    return SessionRow(name, display)


def detail_values(
    name: str, session: Dict[str, Any],
    reg_path: Optional[Path],
) -> Dict[str, str]:
    """Field values for the CONNECTION DETAIL pane. The command is built
    without side effects (no puttygen run, no writes) like the preview
    before it."""
    cmd, _ = build_ssh_command(
        unquote_display(name), session, reg_path=reg_path,
        tmux=False, quiet=True, convert_keys=False,
    )
    return {
        "command": shlex.join(cmd) if cmd else "(no hostname)",
        "HostName": session_value(session, "HostName", "-"),
        "UserName": session_value(session, "UserName", "-"),
        "PortNumber": session_value(session, "PortNumber", "22"),
        "Protocol": session_value(session, "Protocol", "ssh"),
        "PublicKeyFile": session_value(session, "PublicKeyFile", "-"),
        "ProxyHost": jump_host_of(session) or "-",
        "RemoteCommand": session_value(session, "RemoteCommand", "-"),
    }


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


# the canonical (label, key) field list shared with the edit dialog
DETAIL_FIELDS = SESSION_FIELDS


class DetailField(FarValueField):
    """Read-only far-field for the detail pane: the shared value-field
    widget with the pane's .detail-field styling (the value is set
    programmatically as the highlight moves; never focusable)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(classes="detail-field", **kwargs)


class DetailPane(Vertical):
    """The CONNECTION DETAIL pane: a read-only form (SSH command box +
    labeled field rows) refreshed in place when the highlight moves."""


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


class SessionBrowser(KeyBarOwner, App[int]):
    """putty-cli session browser; returns 0 normally, 1 on error."""

    CSS = f"""
    /* FAR chrome, palette-driven (ACTIVE_PALETTE via PUTTY_THEME) */
    .browser-bg {{ background: $background; }}
    ListView {{ height: 1fr; background: $background; color: $foreground; }}
    ListView > ListItem.--highlight {{ background: $primary;
        color: {ACTIVE_PALETTE.highlight_fg}; }}
    #filter {{ dock: top; background: $background; color: $foreground;
             border: solid {ACTIVE_PALETTE.frame}; margin: 0 1; }}
    /* windows keep a 1-unit margin from the screen edges but sit
       flush against each other in the middle */
    #list-col {{ width: 2fr; padding: 0 0 0 1; }}
    /* 5 entry rows + the double border (recents hold max 5) */
    #recent-win {{ height: 7; }}
    #all-win {{ height: 1fr; }}
    #preview-col {{
        width: 1fr;
        padding: 0 1 0 0;
        color: $foreground;
        background: $background;
    }}
    .detail-label {{ width: 13; height: 1; color: $foreground; }}
    .detail-row {{ height: 1; width: 100%; layout: horizontal; }}
    .detail-field {{
        border: none;
        background: {ACTIVE_PALETTE.input_bg};
        color: {ACTIVE_PALETTE.input_fg};
        height: 1;
        width: 100%;
    }}
    #detail-command {{ height: 3; overflow: hidden; }}
    .detail-gap {{ height: 1; }}
    Static#status {{ dock: bottom; height: 1; color: $primary;
                    background: $background; padding: 0 1;
                    /* Textual 8 docks every bottom-docked widget on
                       the LAST row (no stacking): move up one - the
                       keybar's top margin reserves the space */
                    offset: 0 -1; }}
    FARKeyBar {{ dock: bottom; }}
    FARKeyBar#ctx-keybar {{ dock: bottom; }}
    """ + FAR_SHADOW_CSS + FAR_WINDOW_CSS + FAR_KEYBAR_CSS

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "focus_filter", "Find"),
        # priority: must pre-empt the focused ListView's own Enter
        # (select_cursor), else a second 'Selected' fires and double-fires
        # (check_action disables it while a modal is on top, so the
        # modal's own Enter keys still work)
        Binding("enter", "connect", "Connect", priority=True),
        Binding("n", "new", "New Connection"),
        Binding("p", "ping", "Ping"),
        Binding("l", "logs", "View Session Log"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
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
        recent_file: Optional[Path] = None,
    ):
        super().__init__()
        self.usable = usable
        self.recent_names = recent_names
        self.recent_entries = recent_entries
        self.reg_path = reg_path
        self.group_by_tag = group_by_tag
        self.connect_callback = connect_callback  # runs after suspend()
        self.recent_file = recent_file            # renamed on rename
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
        # cheap (size, mtime) stamp for the external-change poller; our
        # own .reg writes update it so they never look external
        self._reg_stamp: Optional[tuple] = None

    def compose(self) -> ComposeResult:
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
                    with DetailPane(id="preview"):
                        for field_label, key in DETAIL_FIELDS:
                            with Horizontal(classes="detail-row"):
                                yield Static(field_label,
                                             classes="detail-label",
                                             markup=False)
                                yield DetailField(id=f"detail-{key}")
                        yield Static("", classes="detail-gap",
                                     markup=False)
                        yield Static("SSH Command",
                                     classes="detail-label", markup=False)
                        yield Static("", classes="detail-field",
                                     id="detail-command", markup=False)
        yield Static("", id="status")
        yield FARKeyBar(
            [("Enter", "Connect"), ("/", "Find"), ("Tab", "Pane"),
             ("n", "New"), ("e", "Edit"), ("d", "Delete"), ("p", "Ping"),
             ("l", "Log"), ("q", "Quit")],
            id="keybar",
        )

    KEYBAR_LIST = [
        ("Enter", "Connect", "connect"),
        ("n", "New", "new"),
        ("/", "Find", "focus_filter"), ("Tab", "Pane", "next_pane"),
        ("e", "Edit", "edit"), ("d", "Delete", "delete"),
        ("p", "Ping", "ping"), ("l", "Log", "logs"), ("q", "Quit", "quit"),
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
    KEYBAR_PING = [
        ("Esc", "Close", None), ("q", "Close", None),
    ]
    KEYBAR_DELETE = [
        ("y", "Delete", None), ("Esc", "Cancel", None),
    ]

    def on_mount(self) -> None:
        install_far_theme(self)
        self.screen.add_class("browser-bg")
        self.recent_rows = build_recent_rows(
            self.usable, self.recent_names, self.recent_entries)
        self.rows = build_all_rows(self.usable, self.group_by_tag)
        self._refresh_list()
        self._suspendable = True  # probed at action time (headless pilots)
        # poll for external .reg changes (the no-TUI menu re-stats per
        # pick; the TUI has no pick loop, so poll on a timer instead)
        self._reg_stamp = file_stamp(self.reg_path) \
            if self.reg_path is not None else None
        if self.reg_path is not None:
            self.set_interval(2.0, self._check_reg_changed)
        # FAR opens on the active panel: recent connections if there are
        # any, else the all-connections pane
        self.query_one(
            "#recents" if self.recent_rows else "#sessions", ListView
        ).focus()

    def _check_reg_changed(self) -> None:
        """Reload the session list when the .reg changed on disk (a
        Windows re-export while the browser is open). A failed re-read
        keeps the last good snapshot; our own writes update the stamp in
        _edit_saved/_delete_confirmed/_created so they stay silent."""
        if self.reg_path is None or self._reg_stamp is None:
            return
        stamp = file_stamp(self.reg_path)
        if stamp == self._reg_stamp:
            return
        self._reg_stamp = stamp
        try:
            reloaded = usable_sessions(
                parse_putty_reg_cached(self.reg_path))
        except Exception:  # noqa: BLE001 - keep the last good snapshot
            self._status("✗ .reg changed but could not be re-read")
            return
        self.usable = reloaded
        self._reload_recents()
        self._rebuild_rows()
        self._status(f"↻ .reg changed - {len(reloaded)} sessions")

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
        elif action == "delete":
            await self._open_delete_modal(name)
        # None = cancelled: nothing to do

    def _open_reg_modal(self, session_name: str, build_modal: Any,
                        keybar: List[Tuple],
                        on_confirm: Any) -> None:
        """Shared rails for modals that edit the .reg file: the session
        must exist and a .reg must be attached, the staleness stamp is
        captured, the keybar swaps for the dialog's keys, and the
        dismiss result is dispatched back on the main loop."""
        if session_name not in self.usable:
            return
        if self.reg_path is None:
            self._status("no .reg file to edit")
            return
        self._edit_stamp = reg_file_stamp(self.reg_path)
        self._set_keybar(keybar)
        modal = build_modal()

        def on_result(payload: Optional[Dict[str, Any]]) -> None:
            self._set_keybar(self.KEYBAR_LIST)
            self.call_later(on_confirm, payload, session_name)

        self.push_screen(modal, callback=on_result)

    async def _open_delete_modal(self, session_name: str) -> None:
        """FAR confirm dialog; the deletion runs through the guarded
        writer once confirmed."""
        from .tui_actions import DeleteConfirmModal
        self._open_reg_modal(
            session_name,
            build_modal=lambda: DeleteConfirmModal(
                unquote_display(session_name),
                host=session_value(self.usable[session_name], "HostName")),
            keybar=self.KEYBAR_DELETE,
            on_confirm=self._delete_confirmed,
        )

    async def _delete_confirmed(
        self, payload: Optional[Dict[str, Any]], session_name: str,
    ) -> None:
        if not payload or not payload.get("delete"):
            self._status("delete cancelled")
            return
        reg_path = self.reg_path
        if reg_path is None:  # guarded when the dialog opened
            self._status("no .reg file to edit")
            return
        try:
            result = delete_session(
                reg_path, session_name, expect_stamp=self._edit_stamp)
        except RegEditError as e:
            self._status(f"✗ not deleted: {e}")
            return
        self.usable.pop(session_name, None)
        self.recent_names = [n for n in self.recent_names
                             if n != session_name]
        self.recent_entries = {k: v for k, v in self.recent_entries.items()
                               if k != session_name}
        self.selected_name = None
        if self.recent_file is not None:
            remove_entry(self.recent_file, session_name)
        self._reg_stamp = file_stamp(reg_path)  # ours: not an external change
        self._rebuild_rows()
        backup = result.backup_path.name if result.backup_path \
            else "no backup"
        self._status(f"deleted ✓ (backup: {backup})")

    async def _open_edit_modal(self, session_name: str) -> None:
        """FAR-style edit dialog over the browser; saves via the surgical
        .reg writer (never regenerating untouched bytes)."""
        from .tui_actions import EditSessionModal
        self._open_reg_modal(
            session_name,
            build_modal=lambda: EditSessionModal(
                session_name, self.usable[session_name],
                reg_dir=self.reg_path.parent
                if self.reg_path is not None else None),
            keybar=self.KEYBAR_EDIT,
            on_confirm=self._edit_saved,
        )

    async def _edit_saved(
        self, payload: Optional[Dict[str, Any]], session_name: str,
    ) -> None:
        if payload is None:
            self._status("edit cancelled")
            return
        rename_to = payload.get("rename_to")
        changes = payload.get("values", {})
        if not changes and not rename_to:
            self._status("saved (nothing to change)")
            return
        reg_path = self.reg_path
        if reg_path is None:  # guarded when the dialog opened
            self._status("no .reg file to edit")
            return
        try:
            result = edit_session_values(
                reg_path, session_name, changes,
                expect_stamp=self._edit_stamp, rename_to=rename_to)
        except RegEditError as e:
            self._status(f"✗ edit not saved: {e}")
            return
        new_name = result.name
        self.usable[new_name] = result.session_values
        renamed = new_name != session_name
        if renamed:
            self.usable.pop(session_name, None)
            self.recent_names = [new_name if n == session_name else n
                                 for n in self.recent_names]
            self.recent_entries = {
                (new_name if k == session_name else k): v
                for k, v in self.recent_entries.items()}
            if self.recent_file is not None:
                rename_entry(self.recent_file, session_name, new_name)
        self._reg_stamp = file_stamp(reg_path)  # ours: not an external change
        self._rebuild_rows()
        backup = result.backup_path.name if result.backup_path \
            else "no backup"
        parts = []
        if renamed:
            parts.append(f"renamed: {unquote_display(new_name)}")
        parts.append(f"saved ✓ (backup: {backup})")
        self._status(" · ".join(parts))

    def _rebuild_rows(self) -> None:
        self.recent_rows = build_recent_rows(
            self.usable, self.recent_names, self.recent_entries)
        self.rows = build_all_rows(self.usable, self.group_by_tag)
        self._refresh_list()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._update_preview()

    def _update_preview(self) -> None:
        """Fill the CONNECTION DETAIL form widgets from the highlight."""
        detail = self.query_one("#preview", DetailPane)
        row = self._current_row()
        fields = [detail.query_one(f"#detail-{key}", DetailField)
                  for _, key in DETAIL_FIELDS]
        if row is None or row.name.startswith("__"):
            detail.query_one(
                "#detail-command", Static).update("select a session")
            for field in fields:
                field.value = ""
            return
        values = detail_values(
            row.name, self.usable[row.name], self.reg_path)
        detail.query_one("#detail-command", Static).update(values["command"])
        for field, (_, key) in zip(fields, DETAIL_FIELDS):
            field.value = values[key]

    def check_action(self, action: str, parameters: tuple) -> bool:
        """Disable bindings while ANY modal screen is active so the
        modal's own widgets and keys keep working (also keeps
        Enter/e/n/p from opening dialogs underneath an open screen).
        isinstance, not a class list: new modals are covered for free."""
        if action in ("connect", "edit", "new", "ping", "delete"):
            return not isinstance(self.screen, ModalScreen)
        return True

    async def action_quit(self) -> None:
        self.selected_name = None
        self.exit(0)

    def _selected_session(self) -> Optional[Tuple[str, Dict[str, Any]]]:
        row = self._current_row()
        if row is None or row.name.startswith("__"):
            return None
        return row.name, self.usable[row.name]

    def _status(self, message: str) -> None:
        """Set the status-line message and repaint it."""
        self.status_message = message
        self._update_status()

    def _require_selected(self) -> Optional[Tuple[str, Dict[str, Any]]]:
        """The highlighted session, or None with a status-line hint."""
        sel = self._selected_session()
        if sel is None:
            self._status("no session highlighted")
        return sel

    def _reload_recents(self) -> None:
        """Re-read the recents file and refresh the RECENT pane: the
        connect callback updated the file inside the suspend; without
        this the pane stays stale until a restart."""
        if self.recent_file is None:
            return
        from .recent_sessions_util import load_recent_entries, load_recent_sessions
        try:
            self.recent_names = load_recent_sessions(
                self.recent_file, self.usable)
            self.recent_entries = load_recent_entries(self.recent_file)
        except Exception:  # nosec B110 - best-effort reload
            pass

    async def action_connect(self) -> None:
        sel = self._require_selected()
        if sel is None:
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
        # putty.py's callback recorded this connect: show it NOW
        self._reload_recents()
        self._rebuild_rows()
        self._status(f"connected: {unquote_display(name)}")

    async def action_ping(self) -> None:
        """p / menu Ping: FAR ping dialog (probe worker fills results).
        Jump-hosted sessions probe THROUGH the jump (the real ssh
        path, BatchMode so a password prompt can't hang it)."""
        sel = self._require_selected()
        if sel is None:
            return
        name, session = sel
        host = session_value(session, "HostName")
        port = session_value(session, "PortNumber", "22")
        if not host:
            self._status("no hostname to ping")
            return
        from .tui_actions import PingModal
        jump = jump_host_of(session)
        probe_factory = None
        if jump:
            def _jump_probe():  # runs in the dialog's worker thread
                from .net_util import run_jump_probe, run_ping
                cmd, _ = build_ssh_command(
                    unquote_display(name), session, reg_path=self.reg_path,
                    tmux=False, quiet=True)
                if not cmd:
                    return run_ping(host, port)
                return run_jump_probe(cmd, jump)
            probe_factory = _jump_probe
        self._set_keybar(self.KEYBAR_PING)
        modal = PingModal(
            unquote_display(name), host, port, jump_host=jump,
            probe_factory=probe_factory,
            on_complete=self._on_ping_complete())

        def on_result(_r: object) -> None:
            self._set_keybar(self.KEYBAR_LIST)

        self.push_screen(modal, callback=on_result)

    def _on_ping_complete(self) -> Any:
        """Mirror the probe result into the status line (persists after
        the dialog closes)."""
        def complete(ok: bool, msg: str) -> None:
            self._status(("✓ " if ok else "✗ ") + msg)
        return complete

    async def action_copy(self) -> None:
        sel = self._require_selected()
        if sel is None:
            return
        name, session = sel
        from .tui_actions import copy_command
        cmd, _ = build_ssh_command(
            unquote_display(name), session, reg_path=self.reg_path,
            tmux=False, quiet=True,
        )
        if not cmd:
            self._status("nothing to copy")
        elif copy_command(shlex.join(cmd)):
            self._status("copied (OSC52/tmux buffer)")
        else:
            self._status("copy failed")

    async def action_delete(self) -> None:
        """d: confirm-then-delete the highlighted session."""
        sel = self._require_selected()
        if sel is None:
            return
        await self._open_delete_modal(sel[0])

    async def action_new(self) -> None:
        """n: create a new connection (blank FAR edit dialog; the
        surgical writer appends the section on save)."""
        if self.reg_path is None:
            self._status("no .reg file to edit")
            return
        from .tui_actions import EditSessionModal
        self._edit_stamp = reg_file_stamp(self.reg_path)
        self._set_keybar(self.KEYBAR_EDIT)
        modal = EditSessionModal(
            None, {}, create=True,
            reg_dir=self.reg_path.parent
            if self.reg_path is not None else None)

        def on_result(payload: Optional[Dict[str, Any]]) -> None:
            self._set_keybar(self.KEYBAR_LIST)
            self.call_later(self._created, payload)

        self.push_screen(modal, callback=on_result)

    async def _created(self, payload: Optional[Dict[str, Any]]) -> None:
        """Save the New Connection dialog: append the section to .reg."""
        if payload is None:
            self._status("create cancelled")
            return
        reg_path = self.reg_path
        if reg_path is None:  # guarded when the dialog opened
            self._status("no .reg file to edit")
            return
        from .putty_reg_write_util import create_session
        try:
            result = create_session(
                reg_path, payload["name"], payload["values"],
                expect_stamp=self._edit_stamp)
        except RegEditError as e:
            self._status(f"✗ not created: {e}")
            return
        self.usable[result.name] = result.session_values
        self._reg_stamp = file_stamp(reg_path)  # ours: not external
        self._rebuild_rows()
        backup = result.backup_path.name if result.backup_path \
            else "no backup"
        self._status(f"created ✓ {unquote_display(result.name)} "
                     f"(backup: {backup})")

    async def action_edit(self) -> None:
        """e: open the edit dialog for the highlighted session."""
        sel = self._require_selected()
        if sel is None:
            return
        await self._open_edit_modal(sel[0])

    async def action_logs(self) -> None:
        """l: the highlighted session's logs; with several, a FAR
        picker opens first, then the chosen one in the log dialog."""
        sel = self._require_selected()
        if sel is None:
            return
        from .logs_util import list_logs
        logs = list_logs(self._logs_dir(), limit=10,
                         session_name=sel[0])
        if not logs:
            self._status(f"no logs yet for {unquote_display(sel[0])}")
            return
        from .tui_actions import LogModal, LogPickerModal
        if len(logs) == 1:
            self.push_screen(LogModal(logs[0][0]))
            return
        self._set_keybar(self.KEYBAR_MENU)

        def on_result(path: Optional[Path]) -> None:
            self._set_keybar(self.KEYBAR_LIST)
            if path is not None:
                self.push_screen(LogModal(path))

        self.push_screen(
            LogPickerModal(logs, unquote_display(sel[0])),
            callback=on_result)

    def _logs_dir(self) -> Path:
        from .logs_util import log_dir
        return log_dir(self.reg_path)
