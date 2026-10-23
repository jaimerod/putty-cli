"""Action helpers used by the TUI (kept importable without a running app)."""
import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, RichLog, Rule, Static

from .clipboard_util import copy_command
from .far_dialog import (
    FarDialogScreen, FarValueField, underlying_browser_screen,
)
from .far_style import ACTIVE_PALETTE, FARKeyBar, KeyBarOwner
from .net_util import run_ping
from .session_util import SESSION_DEFAULTS, SESSION_FIELDS, session_value
from .titlecase_util import unquote_display

__all__ = [
    "SessionActionMenu", "EditSessionModal", "PingModal",
    "DeleteConfirmModal", "LogModal", "run_ping", "copy_command",
]


class SessionActionMenu(FarDialogScreen[Optional[str]]):
    """Cursor-positioned context menu (popup) for one session.

    A small option box opens where the cursor is; arrows navigate,
    Enter executes, Esc or clicking outside closes. The drop shadow
    (from the dialog chassis) is FAR-console-style: a solid black
    backdrop offset +2/+1 with the covered screen copy drawn in it,
    dimmed to the palette's dark-gray.
    Dismiss result: 'connect' | 'ping' | 'copy' | 'logs' | None.
    """

    centered = False
    box_width = None      # width follows the option rows (auto)
    max_box_width = 20    # longest option + padding + border
    BUTTONS = []

    ITEMS = [
        ("Connect", "connect"),
        ("Ping", "ping"),
        ("Copy ssh command", "copy"),
        ("View logs", "logs"),
        ("Edit settings", "edit"),
        ("Delete", "delete"),
    ]

    BINDINGS = [
        ("escape", "cancel", "Close"),
    ]

    DEFAULT_CSS = f"""
    #ctx-items {{
        width: auto;
        height: auto;
        max-height: 16;
    }}

    /* options: popup colors, full-row width */
    .ctx-option {{
        background: {ACTIVE_PALETTE.popup_bg};
        color: {ACTIVE_PALETTE.popup_text};
        padding: 0 2;
        height: 1;
        width: 1fr;
        text-align: center;
    }}

    /* selected: inverted */
    .ctx-option.-selected {{
        background: {ACTIVE_PALETTE.selected_bg};
        color: {ACTIVE_PALETTE.selected_fg};
        text-style: bold;
    }}
    """

    def __init__(self, session_name: str, cursor: tuple,
                 underlying: Any = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session_name = session_name
        self.cursor = cursor
        self._index = 0
        # explicit snapshot source for the shadow; the chassis falls
        # back to the browser when None
        self.underlying = underlying
        # the caption lives in the box's border row (chassis TITLE)
        self.TITLE = session_name

    def compose_body(self):
        with VerticalScroll(id="ctx-items"):
            for label, _action in self.ITEMS:
                yield self._build_option(label)

    def _build_option(self, label: str) -> Static:
        # pad all rows to the longest label so the highlight bar spans
        # the full menu width evenly
        longest = max(len(item_label) for item_label, _ in self.ITEMS)
        return Static(
            label + " " * (longest - len(label)),
            classes="ctx-option", markup=False,
        )

    @property
    def underlying_screen(self) -> Any:
        return self.underlying or underlying_browser_screen(self)

    def _popup_origin(self, box: Any) -> Tuple[int, int]:
        """Open at the cursor, clamped to the screen."""
        box_w = max(len(label) for label, _ in self.ITEMS) + 4
        box_h = len(self.ITEMS) + 2  # + border top/bottom
        size = self.size
        x = min(self.cursor[0] - 1, max(0, size.width - box_w - 1))
        y = min(self.cursor[1], max(0, size.height - box_h - 2))
        return (x, y)

    def on_mount(self) -> None:
        self._render_items()     # highlight the first option
        super().on_mount()

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
        menu = self.query_one("#far-box")
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


class EditSessionModal(FarDialogScreen):
    """FAR dialog mimicking PuTTY's connection settings.

    The session Name field renames the section (URL-quoted by the
    writer); fields cover what putty-cli consumes: HostName, UserName,
    Port, Protocol (ssh/mosh), key/.ppk path, jump host, remote
    command. Dismiss value: {"values": {key: edited_value},
    "rename_to": name-or-None} - only changed fields - or None when
    cancelled.

    The Save path refuses quotes/multi-line values (they do not
    round-trip .reg value lines); everything else is guarded in
    utils/putty_reg_write_util.py.
    """

    box_width = 48
    FOCUS_TARGET = "#edit-Name"
    BUTTONS = [("Save", "screen.save"), ("Cancel", "screen.cancel")]

    # the (label, key) fields the dialog edits - the canonical list
    # shared with the browser's detail pane
    FIELD_DEFS = SESSION_FIELDS

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, session_name: str,
                 session: Dict[str, Any], **kwargs) -> None:
        super().__init__(**kwargs)
        self.session_name = session_name
        self.TITLE = unquote_display(session_name)
        self._session = dict(session)
        self._original = {
            key: session_value(session, key, SESSION_DEFAULTS[key])
            for _, key in self.FIELD_DEFS
        }
        # ProxyMethod drives the putty-cli jump-host convention but is
        # not an editable field: remember it so save can toggle it
        self._orig_method = str(
            session.get("ProxyMethod", "0") or "0").strip()

    def compose_body(self):
        # the session name itself is editable: renaming rewrites the
        # section key in the .reg (URL-quoted by the writer)
        with Horizontal(classes="far-row"):
            yield Static("Name", classes="far-label", markup=False)
            yield Input(value=unquote_display(self.session_name),
                        id="edit-Name")
        for label, key in self.FIELD_DEFS:
            with Horizontal(classes="far-row"):
                yield Static(label, classes="far-label", markup=False)
                yield Input(value=self._original[key],
                            id=f"edit-{key}")
        yield Static("", id="edit-error", markup=False)

    def _focus_next_field(self, current: Input) -> None:
        widgets = list(self.query("Input, .far-btn"))
        try:
            idx = widgets.index(current)
        except ValueError:
            return
        widgets[(idx + 1) % len(widgets)].focus()

    def on_input_submitted(self, event: "Input.Submitted") -> None:
        """Enter inside a field moves to the next control (fields then
        buttons), FAR dialog style."""
        self._focus_next_field(event.input)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            # sync handler that schedules the (async) cancel through the
            # proper action mechanism
            self.call_after_refresh(self.action_cancel)

    async def action_cancel(self) -> None:
        self.dismiss(None)

    def _error(self, msg: str) -> None:
        self.query_one("#edit-error", Static).update(msg)

    def _collect(self) -> Optional[Dict[str, Any]]:
        """Build the save payload; None = validation error (shown)."""
        values: Dict[str, str] = {}
        for _, key in self.FIELD_DEFS:
            values[key] = self.query_one(
                f"#edit-{key}", Input).value.strip()
        try:
            return self._changes_for(values)
        except ValueError as exc:
            self._error(str(exc))
            return None

    def _changes_for(self, values: Dict[str, str]) -> Dict[str, Any]:
        name = self.query_one("#edit-Name", Input).value.strip()
        if "\r" in name or "\n" in name:
            raise ValueError("Name: line breaks are not storable")
        if not name:
            raise ValueError("Name must not be empty")
        rename_to = None
        if name != unquote_display(self.session_name):
            rename_to = name

        # defaults: PuTTY semantics (empty port = 22, protocol = ssh)
        port = values["PortNumber"] or "22"
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("Port must be a number 1-65535")
        values["PortNumber"] = port
        proto = (values["Protocol"] or "ssh").lower()
        if proto not in ("ssh", "mosh"):
            raise ValueError("Protocol must be ssh or mosh")
        values["Protocol"] = proto
        for key, val in values.items():
            if '"' in val:
                raise ValueError(
                    f"{key}: quotes can't be stored in .reg lines")
            if "\r" in val or "\n" in val:
                raise ValueError(f"{key}: multi-line values are not stored")

        # only fields that actually differ become .reg lines
        changes = {
            key: value for key, value in values.items()
            if value != self._original.get(key, "")
        }
        # jump host field drives ProxyMethod 6 (putty-cli convention)
        jump = values["ProxyHost"].strip()
        if jump and self._orig_method != "6":
            changes["ProxyMethod"] = "6"
        elif not jump and self._orig_method == "6":
            changes["ProxyMethod"] = "0"
        return {"values": changes, "rename_to": rename_to}

    async def action_save(self) -> None:
        payload = self._collect()
        if payload is None:
            return
        self.dismiss(payload)


class PingModal(FarDialogScreen):
    """FAR dialog probing the highlighted session's host.

    Opens instantly ('checking…'), runs the real probe in a worker
    thread (ICMP ping with latency, TCP fallback), then fills the
    Result/Note fields and mirrors the outcome into the browser's
    status line. Esc/q closes. A jump-hosted session's failed direct
    probe says so (it cannot see through the tunnel).
    """

    box_width = 46
    FOCUS_TARGET = None
    BUTTONS = []          # no buttons: Esc/q close (bindings below)

    PROGRESS = "checking..."

    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("q", "cancel", "Close"),
    ]

    def __init__(self, session_display: str, host: str, port: str,
                 jump_host: str = "", on_complete: Any = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self._session = session_display
        self.TITLE = session_display
        self._host = host
        self._port = port or "22"
        self._jump_host = jump_host.strip()
        self._on_complete = on_complete or (lambda *a: None)

    def compose_body(self):
        with Horizontal(classes="far-row"):
            yield Static("Host", classes="far-label", markup=False)
            yield FarValueField(value=self._host, id="ping-host")
        with Horizontal(classes="far-row"):
            yield Static("Port", classes="far-label", markup=False)
            yield FarValueField(value=self._port, id="ping-port")
        with Horizontal(classes="far-row"):
            yield Static("Result", classes="far-label", markup=False)
            yield FarValueField(value=self.PROGRESS, id="ping-result")
        with Horizontal(classes="far-row"):
            yield Static("Note", classes="far-label", markup=False)
            yield FarValueField(value="", id="ping-note")

    async def _probe(self) -> None:
        # run_ping is sync (subprocess + socket): keep the UI live by
        # offloading to a thread
        ok, msg = await asyncio.to_thread(
            run_ping, self._host, self._port)
        self.query_one("#ping-result", FarValueField).value = \
            ("\u2713 " if ok else "\u2717 ") + msg
        note = ""
        if not ok and self._jump_host:
            note = (f"direct probe; connects via "
                    f"{self._jump_host}")
        self.query_one("#ping-note", FarValueField).value = note
        self._on_complete(ok, msg)
        # the browser re-rendered behind the dialog (status line/keybar
        # changed): refresh the shadow copy so it never lags the raster
        self.call_after_refresh(self._sync_shadow)

    async def action_cancel(self) -> None:
        self.dismiss(None)

    def on_mount(self) -> None:
        super().on_mount()
        self.run_worker(self._probe(), exclusive=True)


ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"   # CSI sequences (SGR colors, cursor...)
    r"|\x1b\][^\x07\x1b]*(\x07|\x1b\\)?"  # OSC (title, clipboard...)
    r"|\x1b[@-Z\\-_]"               # other C1 escapes
)


def _strip_ansi(text: str) -> str:
    """tmux pipe-pane captures raw terminal bytes incl. SGR/OSC escape
    sequences and CR-overwrites; they render as garbage in the log
    view, so scrub them and collapse \r-carriage returns."""
    return ANSI_RE.sub("", text.replace("\r\n", "\n")).replace("\r", "")


class LogModal(KeyBarOwner, ModalScreen[None]):
    """FAR dialog for ONE session's log: scrollbar view + live filter.

    Like FAR.exe's file viewer, this dialog is OPAQUE and covers the
    screen: no drop-shadow echo, no offset positioning - a single
    raster, so terminal repaints can't ghost the background. Chrome
    (title / search field / log box / match status / key bar) inside
    one bordered view. '/' focuses the search; typing filters to
    matching lines live; n/N jump between matches (with context);
    Esc/q closes.
    """

    BINDINGS = [
        ("slash", "focus_search", "Search"),
        ("escape", "smart_back", "Back"),
        ("q", "dismiss_dialog", "Back"),
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

    DEFAULT_CSS = f"""
    LogModal {{ background: {ACTIVE_PALETTE.bg}; }}

    #log-title {{
        dock: top;
        color: {ACTIVE_PALETTE.title};
        background: {ACTIVE_PALETTE.bg};
        padding: 0 2;
        height: 1;
        text-style: bold;
    }}
    #log-search {{
        dock: top;
        background: {ACTIVE_PALETTE.popup_bg};
        color: {ACTIVE_PALETTE.popup_text};
        border: none;
        height: 1;
        padding: 0 2;
    }}
    #log-view {{
        border: round {ACTIVE_PALETTE.frame};
        background: {ACTIVE_PALETTE.bg};
        margin: 1 1 1 2;
        height: 1fr;
        scrollbar-size-vertical: 2;
        scrollbar-color: {ACTIVE_PALETTE.highlight_bg};
        scrollbar-background: {ACTIVE_PALETTE.bg};
    }}
    #log-status {{
        dock: bottom;
        height: 1;
        background: {ACTIVE_PALETTE.bg};
        color: {ACTIVE_PALETTE.highlight_bg};
        padding: 0 2;
    }}
    RichLog {{
        background: {ACTIVE_PALETTE.bg};
        color: {ACTIVE_PALETTE.text};
    }}
    FARKeyBar {{ dock: bottom; }}
    """

    def compose(self) -> ComposeResult:
        yield Static(f" {self.log_path.name}", id="log-title",
                     markup=False)
        yield Input(
            placeholder="/ search this log (n next match, N previous)",
            id="log-search",
        )
        yield RichLog(id="log-view", wrap=False, highlight=False,
                      max_lines=self.max_lines)
        yield Static("", id="log-status", markup=False)
        yield FARKeyBar(self.KEYBAR_SCROLL, id="keybar")

    def on_mount(self) -> None:
        self._load()
        self.query_one("#log-view", RichLog).focus()

    KEYBAR_SCROLL = [
        ("/", "Search", "focus_search"), ("n", "Next", "next_match"),
        ("N", "Prev", "prev_match"),
        ("Esc", "Back", "smart_back"), ("q", "Back", "dismiss_dialog"),
    ]
    KEYBAR_SEARCH = [
        ("Enter", "Jump", None), ("Esc", "Done", "smart_back"),
        ("n", "Next", "next_match"), ("N", "Prev", "prev_match"),
    ]

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """Context-sensitive footer: search box vs scroll area hotkeys."""
        try:
            if isinstance(event.widget, Input) and \
                    event.widget.id == "log-search":
                self._set_keybar(self.KEYBAR_SEARCH)
            else:
                self._set_keybar(self.KEYBAR_SCROLL)
        except Exception:  # nosec B110 - not mounted yet
            pass

    def _load(self) -> None:
        try:
            text = self.log_path.read_text(errors="replace")
        except OSError as e:
            self.lines = [f"[red]Cannot read {self.log_path}: {e}[/red]"]
        else:
            self.lines = _strip_ansi(text).splitlines()
        self._write_lines(self.lines)
        self._update_status()

    def _write_lines(self, lines: List[Any]) -> None:
        """Write lines (str or Text renderables) into the log view."""
        view = self.query_one("#log-view", RichLog)
        view.clear()
        for line in lines:
            view.write(line)

    def _update_status(self) -> None:
        shown = len(self.lines)
        msg = f"{self.log_path.name}  |  {shown} lines"
        if self.match_positions:
            msg += (f"  |  match {self.match_index + 1}/"
                    f"{len(self.match_positions)}")
        elif self.match_positions == [] and \
                self.query_one("#log-search", Input).value:
            msg += "  |  no matches"
        self.query_one("#log-status", Static).update(msg)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "log-search":
            self._apply_search()

    async def on_input_submitted(self, event: "Input.Submitted") -> None:
        """Enter in the search ('Jump'): go to the first match and hand
        focus back to the log so n/N stay hotkeys."""
        if event.input.id == "log-search":
            self._ensure_matches(self._current_term())
            self._show_match_context(0 if self.match_positions else None)

    # ── match state ──────────────────────────────────────────────────
    # the search term is NEVER cleared: clearing re-runs the live
    # filter and wipes the match list (the old n/N died after the
    # first jump because of exactly that race)

    def _current_term(self) -> str:
        return self.query_one("#log-search", Input).value.strip()

    def _ensure_matches(self, term: str) -> bool:
        if not self.match_positions and term:
            self.match_positions = [
                i for i, line in enumerate(self.lines)
                if term.lower() in line.lower()
            ]
            self.match_index = 0 if self.match_positions else -1
        if not self.match_positions:
            self._update_status()
            return False
        return True

    def _apply_search(self) -> None:
        term = self._current_term()
        if not term:
            self.match_positions = []
            self.match_index = -1
            self._write_lines(self.lines)
            self._update_status()
            return
        self.match_positions = [
            i for i, line in enumerate(self.lines)
            if term.lower() in line.lower()
        ]
        self.match_index = 0 if self.match_positions else -1
        self._write_lines([
            self._mark(i, line) for i, line in enumerate(self.lines)
            if term.lower() in line.lower()
        ])
        self._update_status()

    def _show_match_context(self, index: Optional[int]) -> None:
        """Jump: show a context window around the match and focus the
        log (n/N become hotkeys again)."""
        if index is None:
            self.query_one("#log-view", RichLog).focus()
            self._update_status()
            return
        self.match_index = index
        target = self.match_positions[index]
        radius = 10
        start = max(0, target - radius)
        end = min(len(self.lines), target + radius + 1)
        # bold the line number AND the whole match row (Text, not
        # markup: the log content is never parsed for [tags])
        rows = [Text(f"  {i + 1} {self.lines[i]}")
                for i in range(start, end)]
        rows[target - start] = Text(
            f"{target + 1} {self.lines[target]}", style="bold")
        self._write_lines(rows)
        self._update_status()
        self.query_one("#log-view", RichLog).focus()

    async def action_focus_search(self) -> None:
        self.query_one("#log-search", Input).focus()

    def _mark(self, index: int, line: str) -> Text:
        """Log line with a bold line-number prefix, as a Text
        renderable - the line content is never parsed as markup (log
        text can legitimately contain '[b]'-looking brackets)."""
        return Text.assemble((f"{index + 1}", "bold"), f" {line}")

    async def action_next_match(self) -> None:
        self._jump_match(1)

    async def action_prev_match(self) -> None:
        self._jump_match(-1)

    def _jump_match(self, direction: int) -> None:
        """n/N: cycle the matches of the current term (the term stays in
        the input, so the state never races away)."""
        term = self._current_term()
        if not term:
            self.query_one("#log-status", Static).update(
                "n/N need a search term (press / first)")
            return
        if self._ensure_matches(term):
            self._show_match_context(
                (self.match_index + direction)
                % len(self.match_positions))

    async def action_smart_back(self) -> None:
        """Esc: from the search field -> back to the log (term and
        matches kept); anywhere else -> close the dialog."""
        focused = self.focused or (self.app.focused if self.app else None)
        if isinstance(focused, Input) and focused.id == "log-search":
            self.query_one("#log-view", RichLog).focus()
            self._update_status()
        else:
            await self.action_dismiss_dialog()

    async def action_dismiss_dialog(self, result: object = None) -> None:
        self.dismiss(None)


class DeleteConfirmModal(FarDialogScreen):
    """FAR confirmation dialog: deletes a session from the .reg export.

    Destructive action, so the dialog defaults its focus to CANCEL
    (rendered first); y confirms, Esc/q/n cancel. Dismiss value:
    {"delete": True} on confirm, None on cancel. The actual removal
    runs in putty_reg_write_util.delete_session with the usual rails
    (stamp guard, timestamped .bak, atomic write, re-parse
    validations).
    """

    box_width = 56
    BUTTONS = [("Cancel", "screen.cancel"), ("Delete", "screen.confirm")]

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("q", "cancel", "Cancel"),
        ("n", "cancel", "Cancel"),
        ("y", "confirm", "Delete"),
    ]

    def __init__(self, session_display: str, host: str = "",
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self._session = session_display
        self.TITLE = session_display
        self._host = host.strip()

    def compose_body(self):
        # the button row comes first so default focus (the chassis
        # focuses the first button) is CANCEL - no accidental deletes
        host_part = f", host: {self._host}" if self._host else ""
        yield Static(f"Delete this connection{host_part} from the .reg "
                     "export?", classes="far-text", markup=False)
        yield Rule("horizontal", line_style="solid", classes="far-divider")
        yield Static("Connection logs under the old name stop matching; "
                     "a .bak backup is saved.",
                     classes="far-text-dim", markup=False)

    async def action_confirm(self) -> None:
        self.dismiss({"delete": True})

    async def action_cancel(self) -> None:
        self.dismiss(None)
