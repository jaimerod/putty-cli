from typing import Any, Dict, Iterator, List, Optional, Tuple
from .titlecase_util import smart_title, unquote_display

def make_display_pair(name: str, session: Dict[str, str]) -> tuple[str, str, str]:
    host = unquote_display(session.get("HostName", "???"))
    user = unquote_display(session.get("UserName", ""))
    port = session.get("PortNumber", "22")
    keyhint = " 🔑" if "PublicKeyFile" in session and session["PublicKeyFile"].strip() else ""
    target = f"{user}@" if user else ""
    target += host
    if port != "22":
        target += f":{port}"
    titled_name = smart_title(unquote_display(name))
    display = f"{titled_name:<45} → {target}{keyhint}"
    return (display.lower(), display, name)


def generate_menu_items(
    sessions: Dict[str, Dict[str, Any]], recent_names: List[str]
) -> Iterator[Tuple[str, Optional[str]]]:
    """
    Yields (display_text, session_name) tuples for the terminal menu.

    Args:
        sessions: A dictionary of all usable sessions.
        recent_names: A list of recently used session names.

    Yields:
        Tuples where the first element is the text for the menu and the second
        is the session name, or None for non-selectable items like headers.
    """
    # Header and items for recent sessions
    if recent_names:
        yield "── RECENT SESSIONS ──────────────────────────────────────────────────────────────────────────", None
        for name in recent_names:
            if name in sessions:
                _, display, session_name = make_display_pair(name, sessions[name])
                yield display, session_name
        # Separator if there are other sessions to show
        if len(sessions) > len(recent_names):
            yield "─────────────────────────────────────────────────────────────────────────────────────────────", None

    # Sorted list of all other sessions
    for name in sorted(
        sessions.keys(), key=lambda n: smart_title(unquote_display(n)).lower()
    ):
        if name not in recent_names:
            _, display, session_name = make_display_pair(name, sessions[name])
            yield display, session_name
