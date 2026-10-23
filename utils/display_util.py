from collections.abc import Iterator
from typing import Any, Dict, Optional, Tuple

from .recent_sessions_util import recent_info_suffix
from .session_util import jump_host_of
from .titlecase_util import smart_title, unquote_display


def separator(text: str = "", width: int = 80) -> str:
    """A '── text ────...' header line `width` columns wide (pure).
    Total line length is exactly `width` when text fits; callers that
    size to the live terminal use separator_term()."""
    prefix = f"── {text}" if text else ""
    dashes = max(0, width - len(prefix))
    return f"{prefix}{'─' * dashes}" if text else "─" * width


def terminal_width() -> int:
    """The terminal's column count, at least 80 (impure: queries tty)."""
    import shutil
    return max(80, shutil.get_terminal_size((80, 24)).columns)


def separator_term(text: str = "") -> str:
    """separator() sized to the live terminal (impure wrapper)."""
    return separator(text, terminal_width())


def make_display_pair(name: str, session: Dict[str, Any]) -> Tuple[str, str, str]:
    host = unquote_display(session.get("HostName", "???"))
    user = unquote_display(session.get("UserName", ""))
    port = session.get("PortNumber", "22")
    keyhint = (
        " 🔑" if "PublicKeyFile" in session
        and session["PublicKeyFile"].strip() else "")
    proxyhint = " 🔀" if jump_host_of(session) else ""
    cmdhint = " ⚡" if session.get("RemoteCommand", "").strip() else ""
    target = f"{user}@" if user else ""
    target += host
    if port != "22":
        target += f":{port}"
    titled_name = smart_title(unquote_display(name))
    display = f"{titled_name:<45} → {target}{keyhint}{proxyhint}{cmdhint}"
    return (display.lower(), display, name)


def session_tag(name: str) -> Optional[str]:
    """Extract a group tag from a session name like 'prod:web-1' -> 'prod'.
    None if the name has no 'tag:' prefix."""
    unquoted = unquote_display(name)
    if ":" in unquoted:
        tag = unquoted.split(":", 1)[0].strip().lower()
        if tag:
            return tag
    return None


def generate_menu_items(
    sessions: Dict[str, Dict[str, Any]], recent_names: list,
    group_by_tag: bool = True,
    recent_entries: Optional[Dict[str, Dict[str, object]]] = None,
) -> Iterator[Tuple[str, Optional[str]]]:
    """
    Yields (display_text, session_name) tuples for the terminal menu.

    Args:
        sessions: A dictionary of all usable sessions.
        recent_names: A list of recently used session names.
        group_by_tag: If True, sessions with 'tag:name' names are grouped
            under 'TAG ─' headers; other sessions follow after.
        recent_entries: Optional {'name': {'ts': epoch, 'exit': code}}
            metadata for recents; adds an '(2h ago ✗)' info suffix.

    Yields:
        Tuples where the first element is the text for the menu and the second
        is the session name, or None for non-selectable items like headers.
    """
    # Header and items for recent sessions
    width = terminal_width()
    if recent_names:
        yield separator("RECENT SESSIONS", width), None
        for name in recent_names:
            if name in sessions:
                _, display, session_name = make_display_pair(name, sessions[name])
                if recent_entries and name in recent_entries:
                    info = recent_info_suffix(recent_entries[name])
                    if info:
                        display = f"{display}  ({info})"
                yield display, session_name
        # Separator if there are other sessions to show
        if len(sessions) > len(recent_names):
            yield separator("", width), None

    remaining = [n for n in sorted(
        sessions.keys(), key=lambda n: smart_title(unquote_display(n)).lower()
    ) if n not in recent_names]

    if not group_by_tag:
        for name in remaining:
            _, display, session_name = make_display_pair(name, sessions[name])
            yield display, session_name
        return

    # Sessions with a 'tag:' prefix, grouped under tag headers
    grouped: Dict[str, list] = {}
    ungrouped: list = []
    for name in remaining:
        tag = session_tag(name)
        if tag:
            grouped.setdefault(tag, []).append(name)
        else:
            ungrouped.append(name)

    for tag in sorted(grouped):
        yield separator(tag.upper(), width), None
        for name in grouped[tag]:
            _, display, session_name = make_display_pair(name, sessions[name])
            yield display, session_name
    if grouped and ungrouped:
        yield separator("OTHER", width), None
    for name in ungrouped:
        _, display, session_name = make_display_pair(name, sessions[name])
        yield display, session_name
