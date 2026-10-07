import os
import shlex
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from .ppk_convert_util import convert_ppk_to_openssh

# Root under which Windows drives appear (WSL). Injectable for tests.
WSL_MNT_ROOT = Path("/mnt")


def safe_name(session_name: str) -> str:
    """Sanitize a session name for use in log file names."""
    sanitized = "".join(
        c if c.isalnum() or c in "-_." else "_" for c in session_name
    )
    return sanitized or "session"


def build_ssh_command(
    session_name: str,
    session: Dict[str, str],
    reg_path: Optional[Path] = None,
) -> Tuple[Optional[List[str]], Optional[str]]:
    """Build the tmux command that opens an ssh session.

    Returns (argv, temp_key_path) where argv is a list safe to pass to
    subprocess.run(shell=False). Returns (None, None) if the session has
    no usable HostName.
    """
    if "HostName" not in session or not session["HostName"].strip():
        return None, None

    parts = ["ssh"]

    port = session.get("PortNumber", "22")
    if port != "22":
        parts.extend(["-p", port])

    # ProxyMethod 6 is PuTTY's "local" proxy command type. This tool maps it
    # to ssh's ProxyJump (-J): in practice PuTTY setups exported here use
    # ProxyHost/ProxyPort/ProxyUsername of a jump host, which is exactly
    # what -J models. Methods 1-3 (SOCKS4/5, HTTP CONNECT) are not supported
    # and produce a warning below.
    proxy_method = session.get("ProxyMethod", "0").strip()
    proxy_host = session.get("ProxyHost", "").strip()
    if proxy_method == "6" and proxy_host:
        proxy_port = session.get("ProxyPort", "22").strip()
        proxy_user = session.get("ProxyUsername", "").strip()
        jump = f"{proxy_user}@{proxy_host}" if proxy_user else proxy_host
        if proxy_port and proxy_port != "22":
            jump = f"{jump}:{proxy_port}"
        parts.extend(["-J", jump])
    elif proxy_method in {"1", "2", "3"} and proxy_host:
        print(
            f"  → Warning: proxy type {proxy_method} (SOCKS/HTTP) has no direct "
            f"ssh equivalent; ignoring proxy for {proxy_host}. "
            "Consider ProxyMethod 6 (jump host) in PuTTY instead.",
            file=sys.stderr,
        )

    user = session.get("UserName", "").strip()
    host = session["HostName"].strip()
    if user:
        parts.append(f"{user}@{host}")
    else:
        parts.append(host)

    temp_key = None
    ppk_file_str = session.get("PublicKeyFile", "").strip()
    if ppk_file_str:
        candidates = []
        p = Path(ppk_file_str)
        if p.is_file():
            candidates.append(p)

        rel = (reg_path.parent / ppk_file_str) if reg_path is not None else Path(ppk_file_str)
        if rel.is_file():
            candidates.append(rel)

        # Windows drive-letter path like C:\Users\...: resolve under the
        # WSL mount root (guard guarantees the colon, so split is safe).
        if ':' in ppk_file_str[:3] and '\\' in ppk_file_str:
            drive, rest = ppk_file_str.split(":", 1)
            drive = drive.lower()
            rest = rest.lstrip("\\").replace("\\", "/")
            wsl_p = WSL_MNT_ROOT / drive / rest
            if wsl_p.is_file():
                candidates.append(wsl_p)

        ppk_path = None
        for cand in candidates:
            if cand.is_file():
                ppk_path = cand
                break

        if ppk_path:
            print(f"  → Using key: {ppk_path}")
            converted = convert_ppk_to_openssh(str(ppk_path))
            if converted:
                parts.extend(["-i", converted])
                temp_key = converted
            else:
                print("  → Key conversion failed → will prompt for password")
        else:
            print(f"  → Key file not found: {ppk_file_str}")

    remote_cmd = session.get("RemoteCommand", "").strip()
    if remote_cmd:
        parts.insert(1, "-t")
        parts.append(remote_cmd)

    # Log dir: PUTTY_LOG_DIR overrides; default to the directory holding the
    # .reg file (so it follows whatever file the user configured).
    timestamp = time.strftime("%Y-%m-%d_%H%M")
    log_dir = os.environ.get("PUTTY_LOG_DIR")
    if not log_dir:
        if reg_path is not None:
            log_dir = str(reg_path.parent)
        else:
            log_dir = str(Path.home() / ".putty-cli" / "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = f"{log_dir}/{timestamp}-{safe_name(session_name)}.log"

    # tmux runs its pane command via the user's shell, so both the window
    # name and every piece of shell text must be quoted with shlex.quote.
    argv = [
        "tmux",
        "new-window" if os.environ.get("TMUX") else "new-session",
        "-n", safe_name(session_name),
        " ".join(shlex.quote(a) for a in parts),
        ";",
    ]
    if not os.environ.get("TMUX"):
        # Not in tmux: create a session that self-destroys when detached.
        argv += ["set-option", "destroy-unattached", "on", ";"]
    # In tmux: pipe the new window's output to the log file.
    argv += ["pipe-pane", "-o", f"cat >> {shlex.quote(log_file)}"]

    return argv, temp_key