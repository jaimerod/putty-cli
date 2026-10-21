"""FAR Manager (classic DOS) color scheme + reusable window chrome.

All chrome and every color flow from a Palette (utils/palette_util.py);
the ACTIVE_PALETTE is chosen by PUTTY_THEME at import time (putty.py
loads .env before the TUI, so env vars and .env both work).

Palette extracted from FAR's default color table (16-color VGA/DOS):
  - panels/dialogs/menus on dark blue
  - silver normal text, white bright text
  - cyan = cursor bar / selected row (black text on it)
  - yellow = dialog/menu titles + key-bar numbers
  - gray = dimmed/disabled
  - drop shadow: black at 50% alpha, offset two columns right and one
    row down - the classic DOS double-wide shadow

NOTE: Textual only auto-loads DEFAULT_CSS for a handful of built-in
widget modules; custom widget classes' DEFAULT_CSS is unreliable in
this setup, so all FAR styling is exposed as CSS constants to be
embedded in the app's CSS.
"""
import os
from typing import List, Optional, Sequence, Tuple

from textual import events
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.theme import Theme
from textual.widgets import Static

from .palette_util import Palette, get_palette

# ── FAR classic palette (VGA 6-bit values) ──────────────────────────────────
FAR_BLUE = "#0000AA"      # panel/dialog background
FAR_LIGHTBLUE = "#5555FF" # popup menus / dialog captions background
FAR_AQUA = "#00AAAA"      # cursor bar / selected row
FAR_SILVER = "#AAAAAA"    # normal text
FAR_WHITE = "#FFFFFF"     # bright text
FAR_YELLOW = "#FFFF55"    # titles, key numbers
FAR_GRAY = "#555555"      # dimmed text

def to_textual_theme(p: Palette) -> Theme:
    """Palette -> Textual Theme: fuels the $-style CSS variables."""
    return Theme(
        name=p.name,
        primary=p.highlight_bg,
        secondary=p.title,
        accent=p.accent,
        foreground=p.text,
        background=p.bg,
        surface=p.bg,
        panel=p.panel,
        success=p.success,
        warning=p.warning,
        error=p.error,
        dark=True,
    )

# The active palette is fixed per run (read once, before CSS is built):
# PUTTY_THEME=monokai|far, from the environment or .env (loaded by
# putty.py main before the TUI modules are imported).
ACTIVE_PALETTE = get_palette(os.environ.get("PUTTY_THEME", "far"))
FAR_THEME = to_textual_theme(ACTIVE_PALETTE)

# Drop shadow block styling - include in the app CSS.
def far_shadow_css(p: Palette) -> str:
    return f"""
.far-shadow {{
    position: absolute;
    width: 100%;
    height: 100%;
    offset: 2 1;
    background: {p.window_shadow};
    layer: shadow;
}}
FarWindow {{
    layer: window;
}}
"""

# FAR-style window frame: frame-colored border on the panel color.
# Window captions (border_title) sit inline in the top border row,
# centered - the FAR.exe dialog-title look.
def far_window_css(p: Palette) -> str:
    return f"""
FarWindow {{
    border: double {p.frame};
    background: {p.bg};
    width: 1fr;
    height: 1fr;
    border-title-align: center;
    border-title-color: {p.title};
}}
FarWindow > Static {{
    color: {p.text};
}}
"""

# FAR-style function key bar: slots equally spaced; each slot =
# dark key chip + accented description; clickable.
def far_keybar_css(p: Palette) -> str:
    return f"""
FARKeyBar {{
    dock: bottom;
    height: 1;
    width: 100%;
    layout: horizontal;
    background: {p.bg};
}}

.keybar-slot {{
    width: 1fr;
    height: 1;
    max-height: 1;
    layout: horizontal;
}}

.keybar-key {{
    background: {p.key_bg};
    color: {p.key_text};
    padding: 0 1 0 0;
    height: 1;
    width: auto;
    min-width: 4;
}}

.keybar-desc {{
    background: {p.desc_bg};
    color: {p.desc_text};
    padding: 0 1;
    height: 1;
    width: 1fr;
    overflow: hidden;
}}

.keybar-status {{
    background: {p.bg};
    color: {p.highlight_bg};
    height: 1;
    width: auto;
    padding: 0 1;
}}

.keybar-slot:hover .keybar-key {{
    background: {p.dim};
    color: {p.bright};
}}

.keybar-slot:hover .keybar-desc {{
    background: {p.highlight_bg};
    color: {p.highlight_fg};
}}
"""

FAR_SHADOW_CSS = far_shadow_css(ACTIVE_PALETTE)
FAR_WINDOW_CSS = far_window_css(ACTIVE_PALETTE)
FAR_KEYBAR_CSS = far_keybar_css(ACTIVE_PALETTE)


class KeyBarClicked(Message):
    """Posted when a key bar slot is clicked; bubbles from the slot."""

    def __init__(self, bar: "FARKeyBar", key: str, desc: str,
                 action: Optional[str]) -> None:
        super().__init__()
        self.bar = bar
        self.key = key
        self.desc = desc
        self.action = action

    @property
    def control(self) -> "FARKeyBar":
        return self.bar


class KeyBarSlot(Horizontal, can_focus=False):
    """One equal-width key bar cell: [black/gray key][blue/black desc].

    Clickable: posts KeyBarClicked up through the owning FARKeyBar."""

    DEFAULT_CSS = ""

    def __init__(self, key: str, desc: str,
                 action: Optional[str] = None) -> None:
        super().__init__(Static(key, classes="keybar-key", markup=False),
                         Static(desc, classes="keybar-desc", markup=False),
                         classes="keybar-slot")
        self.key = key
        self.desc = desc
        self.action = action

    def on_click(self, event: events.Click) -> None:
        """Left click on either half of the slot triggers the action."""
        if event.button == 1:
            self.post_message(KeyBarClicked(
                self.parent_bar, self.key, self.desc, self.action))
            event.stop()

    @property
    def parent_bar(self) -> "FARKeyBar":
        parent = self.parent
        while parent is not None and not isinstance(parent, FARKeyBar):
            parent = parent.parent
        if parent is None:  # pragma: no cover - structural invariant
            raise RuntimeError("KeyBarSlot not mounted inside a FARKeyBar")
        return parent


class FARKeyBar(Horizontal):
    """FAR-style function-key bar: equal-width clickable slots.

    Slot = key chip (black bg/gray text) + description (blue bg/black).
    entries: (key, desc) or (key, desc, action). set_status() appends a
    cyan status segment at the right edge."""

    def __init__(self, entries: Sequence[Tuple], **kwargs) -> None:
        super().__init__(**kwargs)
        self._status = ""
        self._entries: List[Tuple] = list(entries)

    def compose(self):
        yield from self._build_slots()

    def _build_slots(self):
        for entry in self._entries:
            key, desc = entry[0], entry[1]
            action = entry[2] if len(entry) > 2 else None
            yield KeyBarSlot(key, desc, action)
        if self._status:
            yield Static(self._status, classes="keybar-status",
                         markup=False)

    def bar_text(self) -> str:
        """Plain text aggregate (tests + debug)."""
        parts = [f"{key} {desc}" for key, desc, *_ in self._entries]
        if self._status:
            parts.append(f"[{self._status}]")
        return "  ".join(parts)

    def _render_slots(self) -> None:
        """Swap entries after mount (call_after_refresh-safe)."""
        self._entries = list(self._entries)
        try:
            self.remove_children()
            self.mount(*self._build_slots())
        except Exception:  # nosec B110 - pre-mount swap; compose covers it
            pass

    def set_entries(self, entries: Sequence[Tuple]) -> None:
        new = [tuple(e) if not isinstance(e, tuple) else e for e in entries]
        if new == self._entries:  # same bar: avoid re-mounting the slots
            return
        self._entries = new
        self._render_slots()

    def set_status(self, status: str) -> None:
        self._status = status
        self._render_slots()


# FAR windows/shadow widget classes (styles live in the CSS constants above).
class FarShadow(Static):
    """Drop shadow block; embed FAR_SHADOW_CSS in the app CSS."""

    def __init__(self) -> None:
        super().__init__("", classes="far-shadow", markup=False)


class FarWindow(Vertical, can_focus=False):
    """FAR-style window: silver frame on blue; embed FAR_WINDOW_CSS.

    title: caption drawn inline in the top border row, centered
    (FAR.exe dialog-title style; empty string = no caption)."""

    def __init__(self, *children, title: str = "", **kwargs) -> None:
        super().__init__(*children, **kwargs)
        self.border_title = title
        self.title_text = title


def install_far_theme(app, palette: Optional[Palette] = None) -> None:
    """Register + activate the palette on a Textual app."""
    theme = to_textual_theme(palette or ACTIVE_PALETTE)
    app.register_theme(theme)
    app.theme = theme.name
