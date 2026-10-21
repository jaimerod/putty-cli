"""Action helpers used by the TUI (kept importable without a running app)."""
import asyncio
import os
import re
import shutil
import socket
import subprocess  # nosec
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, RichLog, Static

from .far_style import ACTIVE_PALETTE, FARKeyBar, KeyBarClicked


def _tcp_probe(host: str, port: str = "22") -> Tuple[bool, str]:
    """TCP connect probe (last-resort reachability check)."""
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            return True, f"{host}:{port} reachable (tcp)"
    except OSError as e:
        return False, f"{host}:{port} unreachable (tcp: {e})"


PING_TIME_RE = re.compile(r"time[=<]([\d.]+)\s*ms", re.IGNORECASE)
_PING_DEAD = (
    "unknown host", "name or service not known", "no address associated",
    "destination host unreachable", "operation not permitted",
    "100% packet loss", "unreachable",)


def run_ping(host: str, port: str = "22") -> Tuple[bool, str]:
    """Reachability check that behaves like a real ping first: one ICMP
    echo via the system ping binary, reporting round-trip latency.

    Falls back to a TCP-port connect probe when ping is missing, not
    permitted, or otherwise unusable. Returns (ok, message)."""
    host = host.split("@")[-1].strip()
    if not host:
        return False, "no hostname to ping"

    ping_bin = shutil.which("ping")
    if ping_bin:
        proc = None
        try:
            proc = subprocess.run(  # nosec: argv list, no shell
                [ping_bin, "-c", "1", "-W", "2", host],
                capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None:
            out = f"{proc.stdout or ''} {proc.stderr or ''}"
            err = out.lower()
            if proc.returncode == 0:
                m = PING_TIME_RE.search(out)
                if m:
                    return True, f"{host}: {m.group(1)} ms"
                return True, f"{host}: reachable"
            if "operation not permitted" in err:
                return _tcp_probe(host, port)  # ping lacks raw-socket caps
            if "unknown host" in err or "name or service not known" in err \
                    or "no address associated" in err:
                return False, f"{host}: unknown host"
            if "unreachable" in err:
                return False, f"{host}: unreachable"
            if "100% packet loss" in err or "packet loss" in err:
                return False, f"{host}: unreachable (no reply)"
            return False, f"{host}: unreachable (ping exit {proc.returncode})"

    return _tcp_probe(host, port)


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


def _compositor_text(screen: Any, x: int, y: int,
                     width: int, height: int) -> Optional[List[str]]:
    """The exact characters the screen's compositor paints in a
    rectangle (window frames, list rows, input lines included).

    Returns None when the raster is unavailable or too short (headless
    edge cases), so callers can fall back to label stamping."""
    compositor = getattr(screen, "_compositor", None)
    if compositor is None:
        return None
    try:
        strips = compositor.render_strips()
    except Exception:  # nosec B110 - compositor not ready
        return None
    lines: List[str] = []
    for strip in strips:
        segs = strip if isinstance(strip, list) \
            else getattr(strip, "segments", strip)
        lines.append("".join(getattr(seg, "text", "") or "" for seg in segs))
    if y + height > len(lines):
        return None
    out: List[str] = []
    for row in lines[y:y + height]:
        if x + width > len(row):
            return None
        out.append(row[x:x + width])
    return out


def _region_text(screen: Any, x: int, y: int,
                 width: int, height: int) -> List[str]:
    """Reconstruct the text shown in a screen rectangle.

    Prefers the underlying screen's composited raster (exact glyphs:
    window frames, list rows, input boxes). Falls back to stamping each
    single-line leaf label's content at its own region.

    This is the data for the drop shadow's FAR-style 'fake
    transparency': the shadow paints a solid black backdrop with the
    covered copy dimmed to FAR dark-gray.
    """
    composited = _compositor_text(screen, x, y, width, height)
    if composited is not None:
        return composited
    grid = [list(" " * width) for _ in range(height)]

    def visit(widget: Any) -> None:
        label = widget.content if isinstance(widget, (Static, Label)) else ""
        region = getattr(widget, "region", None)
        if label and region is not None and region.height == 1:
            ry = region.y - y
            if 0 <= ry < height:
                row = grid[ry]
                text = str(label)
                rx = region.x - x
                for i, ch in enumerate(text):
                    col = rx + i
                    if 0 <= col < width:
                        row[col] = ch
        for child in getattr(widget, "children", ()):
            visit(child)

    try:
        visit(screen)
    except Exception:  # nosec B110 - headless screen not ready
        pass
    return ["".join(row) for row in grid]


def underlying_browser_screen(modal: Any) -> Any:
    """The topmost NON-modal screen below `modal` (e.g. the browser).

    Stacks can nest (menu -> ping -> edit); the shadow's 'copy' must
    always come from the actual background UI, never from another
    dialog."""
    stack = getattr(modal.app, "screen_stack", None) or []
    for screen in reversed(stack):
        if screen is modal:
            continue
        if not isinstance(screen, ModalScreen):
            return screen
    return None


def _sync_far_shadow(box: Any, shadow: Any, underlying: Any,
                     offset: Tuple[int, int] = (2, 1)) -> None:
    """FAR DOS drop shadow for a popup box: opaque black slab (no
    alpha), matched to the box and offset +2/+1, with the covered
    screen copy repainted inside, dimmed to FAR dark-gray.

    No-op when the box hasn't been laid out yet (region still 0x0).
    """
    if not box.region.width:
        return
    sx = box.region.x + offset[0]
    sy = box.region.y + offset[1]
    shadow.styles.width = box.region.width
    shadow.styles.height = box.region.height
    shadow.styles.offset = (sx, sy)
    if underlying is None:
        return
    lines = _region_text(underlying, sx, sy,
                         box.region.width, box.region.height)
    # escape Rich markup chars so echoed content renders verbatim;
    # FAR look: covered glyphs dimmed to dark-gray on the black slab
    from rich.markup import escape as escape_markup
    dimmed = [f"[{ACTIVE_PALETTE.dim}]{escape_markup(line.rstrip())}[/]"
              if line.strip() else "" for line in lines]
    shadow.update("\n".join(dimmed))


class SessionActionMenu(ModalScreen[Optional[str]]):
    """    Cursor-positioned context menu (popup) for one session.

    A small option box opens where the cursor is; arrows navigate,
    Enter executes, Esc or clicking outside closes. The drop shadow
    underneath is FAR-console-style: a solid black backdrop offset
    +2/+1 (no alpha) with the covered screen copy still drawn in it,
    dimmed to FAR dark-gray.
    Dismiss result: 'connect' | 'ping' | 'copy' | 'logs' | None.
    """

    DEFAULT_CSS = f"""
    SessionActionMenu {{ background: transparent !important; }}

    #ctx-menu {{
        layout: vertical;
        /* popup-dialog scheme from the palette: lighter surface,
           frame-colored border */
        background: {ACTIVE_PALETTE.popup_bg};
        border: solid {ACTIVE_PALETTE.frame};
        width: auto;   /* sized to the widest child */
        height: auto;
        padding: 0;
        layer: window;
        box-sizing: border-box;
    }}

    /* children must not claim free space */
    #ctx-menu VerticalScroll {{
        width: auto;
        height: auto;
        max-height: 16;
    }}

    #ctx-title {{
        color: {ACTIVE_PALETTE.popup_text};
        background: {ACTIVE_PALETTE.popup_bg};
        text-style: bold;
        padding: 0 2;
        height: 1;
        width: auto;
    }}

    /* options: full-row width, popup colors */
    .ctx-option {{
        background: {ACTIVE_PALETTE.popup_bg};
        color: {ACTIVE_PALETTE.popup_text};
        padding: 0 2;
        height: 1;
        width: auto;
    }}

    /* selected: inverted */
    .ctx-option.-selected {{
        background: {ACTIVE_PALETTE.selected_bg};
        color: {ACTIVE_PALETTE.selected_fg};
        text-style: bold;
    }}

    /* DOS drop shadow (FAR console): solid black slab offset +2/+1;
       _sync_shadow() sizes it to the box + offset and fills it with
       the covered copy dimmed to the palette's dark-gray */
    #ctx-shadow {{
        position: absolute;
        width: 0;
        height: 0;
        background: {ACTIVE_PALETTE.shadow};
        color: {ACTIVE_PALETTE.dim};
        layer: shadow;
    }}
    """

    BINDINGS = [
        ("escape", "cancel", "Close"),
    ]

    ITEMS = [
        ("Connect", "connect"),
        ("Ping", "ping"),
        ("Copy ssh command", "copy"),
        ("View logs", "logs"),
        ("Edit settings", "edit"),
    ]

    def __init__(self, session_name: str, cursor: tuple[int, int],
                 underlying: Any = None) -> None:
        super().__init__()
        self.session_name = session_name
        self.cursor = cursor
        self._index = 0
        # Screen stack entry directly below this modal: its compositor
        # still holds the raster the menu was opened over, which is what
        # fills the drop shadow (fake transparency).
        self._underlying = underlying

    def _build_option(self, label: str) -> Static:
        # pad all rows to the longest label so the highlight bar spans
        # the full menu width evenly
        longest = max(len(item_label) for item_label, _ in self.ITEMS)
        return Static(
            label + " " * (longest - len(label)),
            classes="ctx-option", markup=False,
        )

    def compose(self) -> ComposeResult:
        yield Static("", id="ctx-shadow", markup=True)
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
        """Shadow matches the menu box exactly, offset +2/+1 (DOS), and
        its text echoes the covered screen copy in FAR dark-gray."""
        try:
            box = self.query_one("#ctx-menu", Vertical)
            shadow = self.query_one("#ctx-shadow", Static)
        except Exception:  # noqa: BLE001 - widgets not mounted yet
            return
        _sync_far_shadow(box, shadow, self._underlying or
                         underlying_browser_screen(self))

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


def _session_str(session: Dict[str, Any], key: str, default: str = "") -> str:
    return str(session.get(key, "") or "").strip() or default


_DEFAULTS = {
    "HostName": "",
    "UserName": "",
    "PortNumber": "22",
    "Protocol": "ssh",
    "PublicKeyFile": "",
    "ProxyHost": "",
    "RemoteCommand": "",
}


class _FarFlatButton(Static, can_focus=True):
    """FAR dialog button: black on light-blue, inverted when focused;
    activates on Enter/Space/click.

    The action string MUST carry the screen namespace ("screen.save") -
    unprefixed actions do NOT bubble from a widget to its screen."""

    def __init__(self, label: str, action: str) -> None:
        super().__init__(f" {label} ", classes="edit-btn", markup=False)
        self._action = action

    async def on_click(self, event: events.Click) -> None:
        if event.button == 1:
            await self._fire()
            event.stop()

    async def on_key(self, event: events.Key) -> None:
        if event.key in ("enter", "space"):
            event.stop()
            event.prevent_default()
            await self._fire()

    async def _fire(self) -> None:
        await self.run_action(self._action)


class EditSessionModal(ModalScreen[Optional[Dict[str, str]]]):
    """FAR-style dialog mimicking PuTTY's connection settings.

    Fields cover what putty-cli consumes: HostName, UserName, Port,
    Protocol (ssh/mosh), key/.ppk path, jump host, remote command.
    Dismiss value: {"values": {key: edited_value}} - only changed
    fields - or {} for a no-op save, or None when cancelled.

    The Save path refuses quotes/multi-line values (they do not
    round-trip .reg value lines); everything else is guarded in
    utils/putty_reg_write_util.py.
    """

    FIELD_DEFS = [
        ("Host", "HostName"),
        ("User", "UserName"),
        ("Port", "PortNumber"),
        ("Protocol", "Protocol"),
        ("Key (.ppk)", "PublicKeyFile"),
        ("Jump host", "ProxyHost"),
        ("Remote cmd", "RemoteCommand"),
    ]

    DEFAULT_CSS = f"""
    EditSessionModal {{ background: transparent; }}

    #edit-shadow {{
        position: absolute;
        width: 0;
        height: 0;
        background: {ACTIVE_PALETTE.shadow};
        color: {ACTIVE_PALETTE.dim};
        layer: shadow;
    }}

    #edit-box {{
        layout: vertical;
        background: {ACTIVE_PALETTE.popup_bg};
        border: round {ACTIVE_PALETTE.frame};
        width: 48;
        height: auto;
        padding: 0;
        layer: window;
    }}

    #edit-title {{
        color: {ACTIVE_PALETTE.popup_text};
        background: {ACTIVE_PALETTE.popup_bg};
        text-style: bold;
        padding: 0 2;
        height: 1;
        width: auto;
    }}

    .edit-row {{
        height: 1;
        width: 100%;
        layout: horizontal;
    }}
    .edit-label {{
        width: 13;
        color: {ACTIVE_PALETTE.popup_text};
        background: {ACTIVE_PALETTE.popup_bg};
        padding: 0 0 0 2;
        height: 1;
    }}
    .edit-row Input {{
        border: none;
        background: {ACTIVE_PALETTE.input_bg};
        color: {ACTIVE_PALETTE.input_fg};
        height: 1;
        width: 1fr;
    }}
    .edit-row Input:focus {{
        background: {ACTIVE_PALETTE.input_focus_bg};
        color: {ACTIVE_PALETTE.input_focus_fg};
        border: none;
    }}

    #edit-error {{
        color: {ACTIVE_PALETTE.error};
        background: {ACTIVE_PALETTE.popup_bg};
        height: 1;
        padding: 0 2;
    }}

    .edit-buttons {{
        height: 1;
        width: 100%;
        layout: horizontal;
    }}
    .edit-btn {{
        height: 1;
        width: auto;
        background: {ACTIVE_PALETTE.popup_bg};
        color: {ACTIVE_PALETTE.popup_text};
        padding: 0 2;
    }}
    .edit-btn:focus {{
        background: {ACTIVE_PALETTE.selected_bg};
        color: {ACTIVE_PALETTE.selected_fg};
        text-style: bold;
    }}
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, session_name: str,
                 session: Dict[str, Any], **kwargs) -> None:
        super().__init__(**kwargs)
        self.session_name = session_name
        self._session = dict(session)
        self._original = {
            key: _session_str(session, key, _DEFAULTS[key])
            for _, key in self.FIELD_DEFS
        }
        # ProxyMethod drives the putty-cli jump-host convention but is
        # not an editable field: remember it so save can toggle it
        self._orig_method = str(
            session.get("ProxyMethod", "0") or "0").strip()

    def compose(self) -> ComposeResult:
        yield Static("", id="edit-shadow", markup=True)
        with Vertical(id="edit-box"):
            yield Label(self.session_name, id="edit-title")
            for label, key in self.FIELD_DEFS:
                with Horizontal(classes="edit-row"):
                    yield Static(label, classes="edit-label", markup=False)
                    yield Input(value=self._original[key],
                                id=f"edit-{key}")
            yield Static("", id="edit-error", markup=False)
            with Horizontal(classes="edit-buttons"):
                yield _FarFlatButton("Save", "screen.save")
                yield _FarFlatButton("Cancel", "screen.cancel")

    def on_mount(self) -> None:
        # FAR dialogs: centered over the browser underneath
        box = self.query_one("#edit-box", Vertical)
        box_h = len(self.FIELD_DEFS) + 5  # title + error + buttons + border
        size = self.size
        box.styles.offset = (
            max(0, (size.width - 48) // 2), max(0, (size.height - box_h) // 2))
        # focus lands AFTER the push settles (call_after_refresh); the
        # modal's own on_mount focus would be overridden by the push
        self.call_after_refresh(self._initial_focus)

    def _initial_focus(self) -> None:
        self.query_one("#edit-HostName", Input).focus()
        self.call_after_refresh(self._sync_shadow)

    def _sync_shadow(self) -> None:
        """Same FAR shadow mechanic as the action menu: echoes the
        covered browser content on the black slab."""
        try:
            box = self.query_one("#edit-box", Vertical)
            shadow = self.query_one("#edit-shadow", Static)
        except Exception:  # noqa: BLE001 - widgets not mounted yet
            return
        _sync_far_shadow(box, shadow, self._underlying_screen)

    @property
    def _underlying_screen(self) -> Any:
        """Screen stack entry below this modal (the browser)."""
        return underlying_browser_screen(self)

    def on_input_submitted(self, event: "Input.Submitted") -> None:
        """Enter inside a field moves to the next control (fields then
        buttons), FAR dialog style."""
        self._focus_next_field(event.input)

    def _focus_next_field(self, current: Input) -> None:
        widgets = list(self.query("Input, _FarFlatButton"))
        try:
            idx = widgets.index(current)
        except ValueError:
            return
        widgets[(idx + 1) % len(widgets)].focus()

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

    def _collect_changes(self) -> Optional[Dict[str, str]]:
        values: Dict[str, str] = {}
        for _, key in self.FIELD_DEFS:
            values[key] = self.query_one(
                f"#edit-{key}", Input).value.strip()
        try:
            return self._changes_for(values)
        except ValueError as exc:
            self._error(str(exc))
            return None

    def _changes_for(self, values: Dict[str, str]) -> Dict[str, str]:
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
        return changes

    async def action_save(self) -> None:
        changes = self._collect_changes()
        if changes is None:
            return
        self.dismiss(changes)


class _PingField(Static, can_focus=False):
    """Read-only field for the ping dialog (styled Static with a value
    property; the TUI's Input has no read-only mode)."""

    def __init__(self, value: str = "", **kwargs: Any) -> None:
        super().__init__(value or "", classes="ping-value", markup=False,
                         **kwargs)

    @property
    def value(self) -> str:
        return str(self.content)

    @value.setter
    def value(self, text: str) -> None:
        self.update(text)


class PingModal(ModalScreen[None]):
    """FAR dialog probing the highlighted session's host.

    Opens instantly ('checking…'), runs the real probe in a worker
    thread (ICMP ping with latency, TCP fallback), then fills the
    Result/Note fields and mirrors the outcome into the browser's
    status line. Esc/q closes. A jump-hosted session's failed direct
    probe says so (it cannot see through the tunnel).
    """

    PROGRESS = "checking…"

    def __init__(self, session_display: str, host: str, port: str,
                 jump_host: str = "", on_complete: Any = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self._session = session_display
        self._host = host
        self._port = port or "22"
        self._jump_host = jump_host.strip()
        self._on_complete = on_complete or (lambda *a: None)

    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("q", "cancel", "Close"),
    ]

    DEFAULT_CSS = f"""
    PingModal {{ background: transparent; }}

    #ping-shadow {{
        position: absolute;
        width: 0;
        height: 0;
        background: {ACTIVE_PALETTE.shadow};
        color: {ACTIVE_PALETTE.dim};
        layer: shadow;
    }}

    #ping-box {{
        layout: vertical;
        background: {ACTIVE_PALETTE.popup_bg};
        border: round {ACTIVE_PALETTE.frame};
        width: 46;
        height: auto;
        padding: 0;
        layer: window;
    }}

    #ping-title {{
        color: {ACTIVE_PALETTE.popup_text};
        background: {ACTIVE_PALETTE.popup_bg};
        text-style: bold;
        padding: 0 2;
        height: 1;
        width: auto;
    }}

    .ping-row {{
        height: 1;
        width: 100%;
        layout: horizontal;
    }}
    .ping-label {{
        width: 12;
        color: {ACTIVE_PALETTE.popup_text};
        background: {ACTIVE_PALETTE.popup_bg};
        padding: 0 0 0 2;
        height: 1;
    }}
    #ping-result, #ping-note, .ping-value {{
        border: none;
        background: {ACTIVE_PALETTE.input_bg};
        color: {ACTIVE_PALETTE.input_fg};
        height: 1;
        width: 1fr;
    }}
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="ping-shadow", markup=False)
        with Vertical(id="ping-box"):
            yield Label(self._session, id="ping-title")
            with Horizontal(classes="ping-row"):
                yield Static("Host", classes="ping-label", markup=False)
                yield _PingField(value=self._host, id="ping-host")
            with Horizontal(classes="ping-row"):
                yield Static("Port", classes="ping-label", markup=False)
                yield _PingField(value=self._port, id="ping-port")
            with Horizontal(classes="ping-row"):
                yield Static("Result", classes="ping-label", markup=False)
                yield _PingField(value=self.PROGRESS, id="ping-result")
            with Horizontal(classes="ping-row"):
                yield Static("Note", classes="ping-label", markup=False)
                yield _PingField(value="", id="ping-note")

    def on_mount(self) -> None:
        self._center_box()
        self.call_after_refresh(self._sync_shadow)
        self.run_worker(self._probe(), exclusive=True)

    def _center_box(self) -> None:
        size = self.size
        self.query_one("#ping-box", Vertical).styles.offset = (
            max(0, (size.width - 46) // 2),
            max(0, (size.height - 10) // 2),
        )

    def _sync_shadow(self) -> None:
        try:
            box = self.query_one("#ping-box", Vertical)
            shadow = self.query_one("#ping-shadow", Static)
        except Exception:  # noqa: BLE001 - widgets not mounted yet
            return
        _sync_far_shadow(box, shadow, self._underlying_screen)

    @property
    def _underlying_screen(self) -> Any:
        """The browser (topmost non-modal screen below this modal)."""
        return underlying_browser_screen(self)

    async def _probe(self) -> None:
        # run_ping is sync (subprocess + socket): keep the UI live by
        # offloading to a thread
        ok, msg = await asyncio.to_thread(
            run_ping, self._host, self._port)
        self.query_one("#ping-result", _PingField).value = \
            ("✓ " if ok else "✗ ") + msg
        note = ""
        if not ok and self._jump_host:
            note = (f"direct probe; connects via "
                    f"{self._jump_host}")
        self.query_one("#ping-note", _PingField).value = note
        self._on_complete(ok, msg)
        # the browser re-rendered behind the dialog (status line/keybar
        # changed): refresh the shadow copy so it never lags the raster
        self.call_after_refresh(self._sync_shadow)

    async def action_cancel(self) -> None:
        self.dismiss(None)


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


class LogModal(ModalScreen[None]):
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
        ("escape", "dismiss_dialog", "Back"),
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
        ("Esc", "Back", "dismiss_dialog"), ("q", "Back", "dismiss_dialog"),
    ]
    KEYBAR_SEARCH = [
        ("Enter", "Apply", None), ("Esc", "Done", None),
        ("n", "Next", "next_match"), ("N", "Prev", "prev_match"),
    ]

    def _set_keybar(self, entries: List[Tuple]) -> None:
        try:
            self.query_one("#keybar", FARKeyBar).set_entries(entries)
        except Exception:  # nosec B110 - not mounted yet
            pass

    async def on_key_bar_clicked(
        self, event: "KeyBarClicked"
    ) -> None:
        act = event.action
        if act is None:
            return
        if hasattr(self, act):
            await self.run_action(act)

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

    def _write_lines(self, lines: List[str]) -> None:
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

    def _apply_search(self) -> None:
        term = self.query_one("#log-search", Input).value.strip()
        if not term:
            self.match_positions = []
            self._write_lines(self.lines)
        else:
            self.match_positions = [
                i for i, line in enumerate(self.lines)
                if term.lower() in line.lower()
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

    def _match_context(self, center: int, radius: int = 10) -> List[str]:
        start = max(0, center - radius)
        end = min(len(self.lines), center + radius + 1)
        return [
            f"[b]{i + 1}[/b] {self.lines[i]}" if i == center
            else f"  {i + 1} {self.lines[i]}"
            for i in range(start, end)
        ]

    async def action_dismiss_dialog(self, result: object = None) -> None:
        self.dismiss(None)
