"""Theme palettes as data - the single source of every TUI color.

Every hardcoded hex in the TUI CSS flows from one of these Palette
entries through `role` fields. Adding a theme = adding a Palette here;
selecting it = PUTTY_THEME=<name> (env or .env, read when the TUI is
imported). Roles map 1:1 onto the FAR chrome: window frames, captions,
menus/dialogs, inputs, the key bar and the DOS shadow.
"""
import os
from dataclasses import dataclass, fields  # noqa: F401 - fields is API
from typing import Dict, Optional


@dataclass(frozen=True)
class Palette:
    name: str
    # backgrounds / text
    bg: str              # panels, windows, scrollbacks
    text: str            # base foreground
    bright: str          # emphasized text
    dim: str             # shadowed copy, comments, muted hints
    # windows
    frame: str           # double-line window borders
    title: str           # window captions, title bar, F-key numbers
    # list cursor bar
    highlight_bg: str
    highlight_fg: str
    # popup menus / dialogs (lighter surface than the panels)
    popup_bg: str
    popup_text: str
    selected_bg: str     # active option row / focused button
    selected_fg: str
    # input fields
    input_bg: str
    input_fg: str
    input_focus_bg: str
    input_focus_fg: str
    # key bar
    key_bg: str          # the key chip
    key_text: str
    desc_bg: str         # the description cell
    desc_text: str
    # drop shadows and accents
    shadow: str          # opaque action-menu/dialog shadow slab
    window_shadow: str   # decorative window shadow (translucent css value)
    error: str
    success: str
    # Textual Theme ($-variable) extras
    panel: str
    accent: str
    warning: str


FAR_PALETTE = Palette(
    name="far",
    bg="#0000AA", text="#AAAAAA", bright="#FFFFFF", dim="#555555",
    frame="#AAAAAA", title="#FFFF55",
    highlight_bg="#00AAAA", highlight_fg="#000000",
    popup_bg="#5555FF", popup_text="#000000",
    selected_bg="#000000", selected_fg="#5555FF",
    input_bg="#000000", input_fg="#FFFFFF",
    input_focus_bg="#000080", input_focus_fg="#FFFF55",
    key_bg="#000000", key_text="#AAAAAA",
    desc_bg="#0055FF", desc_text="#FFFFFF",
    shadow="#000000", window_shadow="#000000 50%",
    error="#FF5555", success="#00AA00",
    panel="#00005A", accent="#5555FF", warning="#FFFF55",
)

MONOKAI_PALETTE = Palette(
    name="monokai",
    bg="#272822", text="#F8F8F2", bright="#FFFFFF", dim="#75715E",
    frame="#75715E", title="#FD971F",
    highlight_bg="#49483E", highlight_fg="#F8F8F2",
    popup_bg="#3E3D32", popup_text="#F8F8F2",
    selected_bg="#F92672", selected_fg="#F8F8F2",
    input_bg="#1E1F1C", input_fg="#F8F8F2",
    input_focus_bg="#3E3D32", input_focus_fg="#E6DB74",
    key_bg="#1E1F1C", key_text="#75715E",
    desc_bg="#49483E", desc_text="#F8F8F2",
    shadow="#000000", window_shadow="#000000 50%",
    error="#F92672", success="#A6E22E",
    panel="#3E3D32", accent="#AE81FF", warning="#FD971F",
)

PALETTES: Dict[str, Palette] = {
    p.name: p for p in (FAR_PALETTE, MONOKAI_PALETTE)
}

DEFAULT_THEME = "far"


def get_palette(name: Optional[str] = None) -> Palette:
    """Resolve a palette by name (missing/empty = the default)."""
    key = (name or DEFAULT_THEME).strip().lower()
    try:
        return PALETTES[key]
    except KeyError:
        known = ", ".join(sorted(PALETTES))
        raise KeyError(
            f"unknown theme {name!r}; known themes: {known}") from None


def active_palette_name() -> str:
    """PUTTY_THEME env value (validated lazily by get_palette users)."""
    return os.environ.get("PUTTY_THEME", DEFAULT_THEME)
