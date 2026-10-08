import re
from urllib.parse import unquote


def unquote_display(s: str) -> str:
    """Decode percent-escapes for nicer display of PuTTY session names
    (PuTTY encodes ' ' as %20, '/' as %2F, etc.)."""
    return unquote(s)


def smart_title(s: str) -> str:
    """Title case after space, hyphen, underscore, or period (no space after period)."""
    return re.sub(r'(^|[\s\-_.])([a-zA-Z])', lambda m: m.group(1) + m.group(2).upper(), s)
