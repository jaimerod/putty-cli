"""Clipboard copy shared by the CLI menu and the TUI."""
import base64
import os
import subprocess  # nosec
import sys


def copy_command(text: str) -> bool:
    """Copy via tmux buffer (inside tmux) or OSC52 escape sequence
    (works in terminals that support it, even over ssh chains)."""
    if os.environ.get("TMUX"):
        try:
            # fixed argv, 'tmux' from PATH; text goes via stdin buffer
            result = subprocess.run(  # nosec B603, B607
                ["tmux", "load-buffer", "-w", "-"],
                input=text.encode(), capture_output=True, timeout=5)
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        encoded = base64.b64encode(text.encode()).decode()
        sys.stdout.write(f"\033]52;c;{encoded}\007")
        sys.stdout.flush()
        return True
    except OSError:
        return False