def unquote_display(s: str) -> str:
    """Clean up %20 etc. for nicer display"""
    return s.replace("%20", " ").replace("%2F", "/").replace("%5C", "\\").replace("%3A", ":")
import re

def smart_title(s: str) -> str:
    """Title case after space, hyphen, underscore, or period (no space after period)."""
    return re.sub(r'(^|[\s\-_.])([a-zA-Z])', lambda m: m.group(1) + m.group(2).upper(), s)
