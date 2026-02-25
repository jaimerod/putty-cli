import os
import shlex
import time
from pathlib import Path
from typing import Dict, Optional, Tuple
from .ppk_convert_util import convert_ppk_to_openssh

REG_PATH = Path("/mnt/c/Users/hiiam/OneDrive/Shared/portable/PuTTY/Data/settings/putty.reg").expanduser().resolve()

def build_ssh_command(session_name: str, session: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    if "HostName" not in session or not session["HostName"].strip():
        return None, None

    parts = ["ssh"]

    port = session.get("PortNumber", "22")
    if port != "22":
        parts.extend(["-p", port])

    proxy_method = session.get("ProxyMethod", "0").strip()
    proxy_host = session.get("ProxyHost", "").strip()
    if proxy_method == "6" and proxy_host:
        proxy_port = session.get("ProxyPort", "22").strip()
        proxy_user = session.get("ProxyUsername", "").strip()
        jump = f"{proxy_user}@{proxy_host}" if proxy_user else proxy_host
        if proxy_port and proxy_port != "22":
            jump = f"{jump}:{proxy_port}"
        parts.extend(["-J", jump])

    user = session.get("UserName", "").strip()
    host = session["HostName"].strip()
    if user:
        parts.append(f"{user}@{host}")
    else:
        parts.append(host)

    proto = session.get("Protocol", "ssh").lower()
    if proto == "ssh1":
        parts.insert(1, "-1")
    elif proto == "ssh2":
        parts.insert(1, "-2")

    temp_key = None
    ppk_file_str = session.get("PublicKeyFile", "").strip()
    if ppk_file_str:
        candidates = []
        p = Path(ppk_file_str)
        if p.is_file():
            candidates.append(p)

        rel = REG_PATH.parent / ppk_file_str
        if rel.is_file():
            candidates.append(rel)

        if ':' in ppk_file_str[:3] and '\\' in ppk_file_str:
            try:
                drive, rest = ppk_file_str.split(":", 1)
                drive = drive.lower()
                rest = rest.lstrip("\\").replace("\\", "/")
                wsl_p = Path(f"/mnt/{drive}") / rest
                if wsl_p.is_file():
                    candidates.append(wsl_p)
            except:
                pass

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
        parts.append(f'"{remote_cmd}"')

    # Build the base SSH command
    base_cmd = " ".join(parts)
    safe_name = session_name.replace("'", "")

    # Create a dedicated logs directory and a timestamped filename
    timestamp = time.strftime("%Y-%m-%d_%H%M")
    log_dir = '/mnt/c/Users/hiiam/OneDrive/Shared/portable/PuTTY'
    os.makedirs(log_dir, exist_ok=True)
    log_file = f"{log_dir}/{timestamp}-{safe_name}.log"

    # Check if we are currently inside a tmux session
    if os.environ.get("TMUX"):
        # In tmux: Open new window and instantly start piping output to the log file
        cmd = f"tmux new-window -n '{safe_name}' '{base_cmd}' \\; pipe-pane -o 'cat >> \"{log_file}\"'"
    else:
        # Not in tmux: Create wrapper, set auto-destroy, and pipe output
        cmd = f"tmux new-session -n '{safe_name}' '{base_cmd}' \\; set-option destroy-unattached on \\; pipe-pane -o 'cat >> \"{log_file}\"'"

    return cmd, temp_key
