"""FAR Manager (classic DOS) color scheme + reusable window chrome.

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
from typing import Sequence, Tuple

from textual.containers import Vertical
from textual.theme import Theme
from textual.widgets import Static

# ── FAR classic palette (VGA 6-bit values) ──────────────────────────────────
FAR_BLUE = "#0000AA"      # panel/dialog background
FAR_LIGHTBLUE = "#5555FF" # popup menus / dialog captions background
FAR_AQUA = "#00AAAA"      # cursor bar / selected row
FAR_SILVER = "#AAAAAA"    # normal text
FAR_WHITE = "#FFFFFF"     # bright text
FAR_YELLOW = "#FFFF55"    # titles, key numbers
FAR_GRAY = "#555555"      # dimmed text

FAR_THEME = Theme(
    name="far",
    primary=FAR_AQUA,
    secondary=FAR_YELLOW,
    accent="#5555FF",
    foreground=FAR_SILVER,
    background=FAR_BLUE,
    surface=FAR_BLUE,
    panel="#00005A",
    success="#00AA00",
    warning=FAR_YELLOW,
    error="#FF5555",
    dark=True,
)

# Drop shadow block styling - include in the app CSS.
FAR_SHADOW_CSS = """
.far-shadow {
    position: absolute;
    width: 100%;
    height: 100%;
    offset: 2 1;
    background: #000000 50%;
    layer: shadow;
}
FarWindow {
    layer: window;
}
"""

# FAR-style window frame: silver double-line border on blue.
FAR_WINDOW_CSS = """
FarWindow {
    border: double #AAAAAA;
    background: #0000AA;
    width: 1fr;
    height: 1fr;
}
FarWindow .far-window-title {
    color: #FFFF55;
    background: #0000AA;
    padding: 0 1;
    height: 1;
}
FarWindow > Static {
    color: #AAAAAA;
}
"""

# FAR-style function key bar (bottom line).
FAR_KEYBAR_CSS = """
FARKeyBar {
    dock: bottom;
    height: 1;
    background: #0000AA;
    color: #AAAAAA;
}
"""


def install_far_theme(app) -> None:
    """Register + activate the FAR theme on a Textual app."""
    app.register_theme(FAR_THEME)
    app.theme = "far"


class FarShadow(Static):
    """Drop shadow block; styling comes from FAR_SHADOW_CSS (embed in
    the app CSS) since custom-class DEFAULT_CSS is not auto-loaded."""

    def __init__(self) -> None:
        super().__init__("", classes="far-shadow", markup=False)


class FarWindow(Vertical, can_focus=False):
    """FAR-style window: silver double-line frame on blue. Give it an
    id and include FAR_WINDOW_CSS in the app's CSS."""

    def __init__(self, *children, title: str = "", **kwargs) -> None:
        super().__init__(*children, **kwargs)
        self.title_text = title


class FARKeyBar(Static):
    """FAR-style function-key bar (bottom line): key names yellow,
    labels silver, optional status message on the right in cyan."""

    def __init__(self, entries: Sequence[Tuple[str, str]], **kwargs) -> None:
        super().__init__(markup=True, **kwargs)
        self.entries = list(entries)
        self.status = ""
        self.update(self._build())

    def set_entries(self, entries: Sequence[Tuple[str, str]]) -> None:
        """Swap the key/label set (context-sensitive hotkey hints)."""
        self.entries = list(entries)
        self.update(self._build())

    def _build(self) -> str:
        key_width = max((len(k) for k, _ in self.entries), default=1)
        parts = []
        for key, label in self.entries:
            parts.append(f"[#FFFF55]{key:<{key_width}}[/] [#AAAAAA]{label}[/]")
        line = "  ".join(parts)
        if self.status:
            line += f"   [#00AAAA]{self.status}[/]"
        return line

    def set_status(self, status: str) -> None:
        self.status = status
        self.update(self._build())

    def update_status_binding(self, message: str) -> None:
        """Show the current status message on the key bar (cyan)."""
        self.set_status(message)
