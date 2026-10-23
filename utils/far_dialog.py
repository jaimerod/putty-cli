"""Reusable FAR-dialog chassis: one tested geometry for every modal.

Every modal previously hand-rolled box sizing (hardcoded height
estimates), shadow placement and one-off button CSS - and each drifted
into content overrun / wrong-shadow bugs. FarDialogScreen provides
ONCE, for all modals:

- a dialog box whose width is fixed per class (`box_width`, set as an
  inline style - no DEFAULT_CSS collisions) and whose height follows
  the content (rows are expected to wrap, never overflow)
- TWO-STAGE placement: the box lays out, then the screen centers it
  from the MEASURED region (no hand height estimates), then the DOS
  drop shadow is synced from the FINAL region (+2/+1, opaque black,
  echoing the underlying browser's copy)
- a button row (`.far-btn` widgets) that lives INSIDE the box
- uniform selectors: #far-box / #far-shadow / .far-btn

Subclasses provide `compose_body()` (rows inside the box) and may set
`centered = False` and override `_place()` for custom positioning
(e.g. cursor-anchored popups).
"""
from typing import Any, Generic, List, Optional, Tuple, TypeVar

from textual import events
from textual.containers import Horizontal, Vertical
from textual.geometry import Region
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from .far_style import ACTIVE_PALETTE

#: the DOS shadow displacement
SHADOW_OFFSET = (2, 1)

DismissType = TypeVar("DismissType")


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

    Stacks can nest (menu -> ping); a dialog's shadow 'copy' must
    always come from the actual background UI, never another dialog."""
    stack = getattr(modal.app, "screen_stack", None) or []
    for screen in reversed(stack):
        if screen is modal:
            continue
        if not isinstance(screen, ModalScreen):
            return screen
    return None


def sync_far_shadow_region(region: Region, shadow: Any, underlying: Any,
                           offset: Tuple[int, int] = SHADOW_OFFSET) -> None:
    """FAR DOS drop shadow: opaque black slab matched to the box region
    and offset +2/+1; the covered screen copy repainted inside, dimmed
    to the palette's dark-gray. No-op before first layout (region 0x0)."""
    if not region.width:
        return
    sx = region.x + offset[0]
    sy = region.y + offset[1]
    shadow.styles.width = region.width
    shadow.styles.height = region.height
    shadow.styles.offset = (sx, sy)
    if underlying is None:
        return
    lines = _region_text(underlying, sx, sy, region.width, region.height)
    from rich.markup import escape as escape_markup
    dimmed = [f"[{ACTIVE_PALETTE.dim}]{escape_markup(line.rstrip())}[/]"
              if line.strip() else "" for line in lines]
    shadow.update("\n".join(dimmed))


class FarValueField(Static, can_focus=False):
    """Read-only value field (Textual's Input has no read-only mode):
    a field-styled Static with a value property, set programmatically.
    `classes` overrides the default .far-value styling (e.g. the detail
    pane's .detail-field look)."""

    def __init__(self, value: str = "", classes: str = "far-value",
                 **kwargs: Any) -> None:
        super().__init__(value or "", classes=classes, markup=False,
                         **kwargs)

    @property
    def value(self) -> str:
        return str(self.content)

    @value.setter
    def value(self, text: str) -> None:
        self.update(text)


class FarFlatButton(Static, can_focus=True):
    """FAR dialog button: popup colors, inverted when focused; fires a
    screen-namespace action on Enter/Space/click.

    The action string MUST carry the screen namespace ("screen.save") -
    unprefixed actions do not bubble from a widget to its screen."""

    def __init__(self, label: str, action: str) -> None:
        super().__init__(f" {label} ", classes="far-btn", markup=False)
        self._action = action

    async def on_click(self, event: events.Click) -> None:
        if event.button == 1:
            await self.fire_action()
            event.stop()

    async def on_key(self, event: events.Key) -> None:
        if event.key in ("enter", "space"):
            event.stop()
            event.prevent_default()
            await self.fire_action()

    async def fire_action(self) -> None:
        await self.run_action(self._action)


class FarDialogScreen(ModalScreen[DismissType], Generic[DismissType]):
    """Base for FAR-style modal dialogs. See the module docstring."""

    centered: bool = True
    box_width: Optional[int] = 62
    #: cap for auto-width boxes (None = fixed width via box_width);
    #: the box is otherwise shrunk to its content in _measure()
    max_box_width: Optional[int] = None
    #: caption rendered centered inside the box's top border row
    #: (the FAR.exe dialog-title look; empty = no caption)
    TITLE: str = ""
    #: (label, screen-namespaced action) rendered inside the box
    BUTTONS: List[Tuple[str, str]] = []
    #: css class of the widget to focus after mount (defaults to the
    #: first button, else #far-box content)
    FOCUS_TARGET: Optional[str] = None

    DEFAULT_CSS = f"""
    FarDialogScreen {{
        /* paint order matters: the shadow is BEHIND the dialog box
           but ABOVE the screen background - Textual only orders
           layers declared here (default layers are just 'default') */
        layers: shadow window default;
        background: transparent;
    }}

    #far-shadow {{
        position: absolute;
        width: 0;
        height: 0;
        background: {ACTIVE_PALETTE.shadow};
        color: {ACTIVE_PALETTE.dim};
        layer: shadow;
        /* shadow copy is markup ON (the echoed lines carry the dim
           color tag); text wraps are clipped hard at the region edge */
        overflow-x: hidden;
        overflow-y: hidden;
    }}

    #far-box {{
        background: {ACTIVE_PALETTE.popup_bg};
        border: round {ACTIVE_PALETTE.frame};
        border-title-align: center;
        border-title-color: {ACTIVE_PALETTE.title};
        width: auto;
        height: auto;
        max-height: 90%;
        padding: 0;
        layer: window;
        layout: vertical;
        /* invisible until _place() applies the final offset - the
           box would otherwise flash in the top-left corner for one
           frame before the measured centering lands (layout still
           runs, so the region/measure stages keep working) */
        opacity: 0;
    }}

    .far-row {{
        height: 1;
        width: 100%;
        layout: horizontal;
    }}
    .far-label {{
        width: 13;
        color: {ACTIVE_PALETTE.popup_text};
        background: {ACTIVE_PALETTE.popup_bg};
        padding: 0 0 0 2;
        height: 1;
    }}
    .far-title {{
        color: {ACTIVE_PALETTE.popup_text};
        background: {ACTIVE_PALETTE.popup_bg};
        text-style: bold;
        padding: 0 2;
        height: 1;
        width: 100%;
        text-overflow: ellipsis;
    }}
    .far-text {{
        color: {ACTIVE_PALETTE.popup_text};
        background: {ACTIVE_PALETTE.popup_bg};
        padding: 0 2;
        width: 100%;
        height: auto;
    }}
    .far-text-dim {{
        color: {ACTIVE_PALETTE.dim};
        background: {ACTIVE_PALETTE.popup_bg};
        padding: 0 2;
        width: 100%;
        height: auto;
    }}
    .far-error {{
        color: {ACTIVE_PALETTE.error};
        background: {ACTIVE_PALETTE.popup_bg};
        padding: 0 2;
        width: 100%;
        height: 1;
    }}
    .far-value {{
        border: none;
        background: {ACTIVE_PALETTE.input_bg};
        color: {ACTIVE_PALETTE.input_fg};
        height: 1;
        width: 1fr;
    }}
    .far-row Input {{
        border: none;
        background: {ACTIVE_PALETTE.input_bg};
        color: {ACTIVE_PALETTE.input_fg};
        height: 1;
        width: 1fr;
    }}
    .far-row Input:focus {{
        background: {ACTIVE_PALETTE.input_focus_bg};
        color: {ACTIVE_PALETTE.input_focus_fg};
        border: none;
    }}

    #far-buttons {{
        height: 1;
        width: 100%;
        layout: horizontal;
        /* FAR dialogs center the button row under the text */
        align: center middle;
    }}
    .far-divider {{
        color: {ACTIVE_PALETTE.frame};
        background: {ACTIVE_PALETTE.popup_bg};
        height: 1;
        margin: 0;
        width: 1fr;
    }}
    .far-btn {{
        height: 1;
        width: auto;
        min-width: 10;
        content-align: center middle;
        background: {ACTIVE_PALETTE.popup_bg};
        color: {ACTIVE_PALETTE.popup_text};
        padding: 0 2;
    }}
    .far-btn:focus {{
        background: {ACTIVE_PALETTE.selected_bg};
        color: {ACTIVE_PALETTE.selected_fg};
        text-style: bold;
    }}
    """

    def compose(self):
        yield Static("", id="far-shadow", markup=True)
        with Vertical(id="far-box"):
            yield from self.compose_body()
            if self.BUTTONS:
                with Horizontal(id="far-buttons"):
                    for label, action in self.BUTTONS:
                        yield FarFlatButton(label, action)

    def compose_body(self):
        """Rows rendered inside the dialog box."""
        return []  # pragma: no cover - subclass hook

    def on_mount(self) -> None:
        box = self.query_one("#far-box")
        # stage 1: caption into the border row + fix the box width;
        # the content lays out from these
        if self.TITLE:
            box.border_title = self.TITLE
        width = self.box_width
        if width is None and self.max_box_width is not None:
            # auto width capped: the box must never span the screen
            # (#far-box is a block in a vertical screen layout, so it
            # would otherwise grow to 100%)
            width = self.max_box_width
        if width is not None:
            box.styles.width = width
        # stage 2 (after the box measured itself): position from the
        # REAL region, then sync the shadow from the FINAL region
        self.call_after_refresh(self._measure)
        self.call_after_refresh(self._place)
        self.call_after_refresh(self._initial_focus)

    def _measure(self) -> None:
        """After the first layout: shrink an auto-width box to its
        content (border title included) so cursor menus and popups size
        to their rows instead of the screen width."""
        if self.box_width is None:
            box = self.query_one("#far-box")
            content_w = max(
                (child.region.width for child in box.children),
                default=0,
            ) + box.styles.gutter.width
            if self.max_box_width is not None:
                content_w = min(content_w, self.max_box_width)
            if content_w:
                box.styles.width = content_w

    def _place(self) -> None:
        """Position the box using its MEASURED region (no estimates)."""
        box = self.query_one("#far-box")
        if self.centered:
            size = self.size
            x = max(0, (size.width - box.region.width) // 2)
            y = max(0, (size.height - box.region.height) // 2)
        else:
            x, y = self._popup_origin(box)
        box.styles.offset = (x, y)
        # reveal at the final position: the paint that shows the box
        # already carries the offset (no top-left flash)
        box.styles.opacity = 1
        # the compositor map still holds the pre-offset origin here;
        # remember the TARGET region so the shadow lands at
        # box + (2, 1) instead of the stale (0, 0)
        self._box_region = box.region.at_offset((x, y))
        # sync the shadow NOW, in the same callback batch: deferring it
        # one refresh let a paint slip in between in real terminals, so
        # the black slab popped in a frame after the box (a visible
        # flash). _box_region carries the final geometry and the
        # underlying raster is already painted, so nothing needs to
        # wait for.
        self._sync_shadow()

    def _popup_origin(self, box: Any) -> Tuple[int, int]:
        """Subclasses with centered=False override this (cursor menus)."""
        return (0, 0)

    def _sync_shadow(self) -> None:
        try:
            box = self.query_one("#far-box")
            shadow = self.query_one("#far-shadow", Static)
        except Exception:  # noqa: BLE001 - widgets not mounted yet
            return
        region = getattr(self, "_box_region", None) or box.region
        sync_far_shadow_region(region, shadow, self.underlying_screen)

    @property
    def underlying_screen(self) -> Any:
        return underlying_browser_screen(self)

    def _initial_focus_is_safe(self, widget: Any) -> bool:
        return True

    def _initial_focus(self) -> None:
        """Focus FOCUS_TARGET if set, else the first .far-btn, else the
        box's focus chain default."""
        try:
            if self.FOCUS_TARGET:
                self.query_one(self.FOCUS_TARGET).focus()
            elif self.BUTTONS:
                for w in self.query(".far-btn"):
                    w.focus()
                    break
        except Exception:  # noqa: BLE001 - selector may not exist
            pass
        self.call_after_refresh(self._sync_shadow)
