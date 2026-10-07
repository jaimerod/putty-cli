from typing import Any, Dict, Iterator, Optional, Tuple
from .titlecase_util import smart_title, unquote_display


def make_display_pair(name: str, session: Dict[str, Any]) -> Tuple[str, str, str]:
    host = unquote_display(session.get("HostName", "???"))
    user = unquote_display(session.get("UserName", ""))
    port = session.get("PortNumber", "22")
    keyhint = " 🔑" if "PublicKeyFile" in session and session["PublicKeyFile"].strip() else ""
    proxyhint = " 🔀" if session.get("ProxyMethod", "0").strip() == "6" and session.get("ProxyHost", "").strip() else ""
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
) -> Iterator[Tuple[str, Optional[str]]]:
    """
    Yields (display_text, session_name) tuples for the terminal menu.

    Args:
        sessions: A dictionary of all usable sessions.
        recent_names: A list of recently used session names.
        group_by_tag: If True, sessions with 'tag:name' names are grouped
            under 'TAG ─' headers; other sessions follow after.

    Yields:
        Tuples where the first element is the text for the menu and the second
        is the session name, or None for non-selectable items like headers.
    """
    # Header and items for recent sessions
    if recent_names:
        yield "── RECENT SESSIONS ───────────────────────────────────────────────────────────", None
        for name in recent_names:
            if name in sessions:
                _, display, session_name = make_display_pair(name, sessions[name])
                yield display, session_name
        # Separator if there are other sessions to show
        if len(sessions) > len(recent_names):
            yield "──────────────────────────────────────────────────────────────────────────────", None

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
        yield f"── {tag.upper()} ──", None
        for name in grouped[tag]:
            _, display, session_name = make_display_pair(name, sessions[name])
            yield display, session_name
    if grouped and ungrouped:
        yield "── OTHER ──", None
    for name in ungrouped:
        _, display, session_name = make_display_pair(name, sessions[name])
        yield display, session_name