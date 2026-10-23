"""Pure helpers over parsed PuTTY session values.

Every consumer of session fields (ssh builder, menu, TUI detail pane,
edit dialog) shares the canonical field list, defaults, and accessors
from here - one definition instead of five parallel ones."""
from typing import Any, Dict, Tuple

#: one parsed session: {registry key: normalized value}
SessionData = Dict[str, str]
#: all parsed sessions keyed by session name
SessionMap = Dict[str, SessionData]

#: (label, key) of the session fields putty-cli shows and edits, in
#: display order - shared by the TUI detail pane and the edit dialog
SESSION_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("Host", "HostName"),
    ("User", "UserName"),
    ("Port", "PortNumber"),
    ("Protocol", "Protocol"),
    ("Key (.ppk)", "PublicKeyFile"),
    ("Jump host", "ProxyHost"),
    ("Remote cmd", "RemoteCommand"),
)

#: normalized defaults (PuTTY semantics: empty port = 22, protocol = ssh)
SESSION_DEFAULTS: Dict[str, str] = {
    "HostName": "",
    "UserName": "",
    "PortNumber": "22",
    "Protocol": "ssh",
    "PublicKeyFile": "",
    "ProxyHost": "",
    "RemoteCommand": "",
}


def session_value(session: Dict[str, Any], key: str,
                   default: str = "") -> str:
    """A session value, stripped; `default` when missing or empty."""
    return str(session.get(key, "") or "").strip() or default


def jump_host_of(session: Dict[str, Any]) -> str:
    """The jump host when ProxyMethod is 6 (PuTTY's 'local' proxy type,
    which putty-cli maps to ssh -J / ProxyJump), else ''."""
    if session_value(session, "ProxyMethod", "0") == "6":
        return session_value(session, "ProxyHost")
    return ""


def usable_sessions(sessions: Dict[str, Dict[str, Any]]) -> SessionMap:
    """Filter sessions to those with a connectable HostName (PuTTY
    shows non-connectable bookkeeping sessions like 'Default Settings'
    too - those have no HostName and cannot be launched). Pure."""
    return {
        k: v
        for k, v in sessions.items()
        if "HostName" in v and v["HostName"].strip()
    }