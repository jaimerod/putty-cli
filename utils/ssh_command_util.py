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


def _convert_session_key(
    ppk_file_str: str, reg_path: Optional[Path], quiet: bool = False
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a PublicKeyFile value and convert it to OpenSSH format.

    Returns (converted_key_path_or_None, temp_key_or_None): temp_key is
    set when the result is a throwaway temp file (not cache-managed).
    quiet=True suppresses status prints (preview mode).
    """
    ppk_path = _resolve_ppk_path(ppk_file_str, reg_path)
    if not ppk_path:
        if not quiet:
            print(f"  → Key file not found: {ppk_file_str}")
        return None, None
    if not quiet:
        print(f"  → Using key: {ppk_path}")
    converted = convert_ppk_to_openssh(str(ppk_path))
    if converted:
        return converted, converted
    if not quiet:
        print("  → Key conversion failed → will prompt for password")
    return None, None


def _resolve_ppk_path(ppk_file_str: str, reg_path: Optional[Path]) -> Optional[Path]:
    """Find an existing .ppk file: absolute path, reg-relative, or
    Windows drive-letter path under the WSL mount root."""
    candidates = []
    p = Path(ppk_file_str)
    if p.is_file():
        candidates.append(p)

    rel = (reg_path.parent / ppk_file_str) if reg_path is not None else Path(ppk_file_str)
    if rel.is_file():
        candidates.append(rel)

    # Windows drive-letter path like C:\Users\...: resolve under the
    # WSL mount root.
    if ':' in ppk_file_str[:3] and '\\' in ppk_file_str:
        drive, rest = ppk_file_str.split(":", 1)
        drive = drive.lower()
        rest = rest.lstrip("\\").replace("\\", "/")
        wsl_p = WSL_MNT_ROOT / drive / rest
        if wsl_p.is_file():
            candidates.append(wsl_p)

    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def build_ssh_command(
    session_name: str,
    session: Dict[str, str],
    reg_path: Optional[Path] = None,
    tmux: bool = True,
    quiet: bool = False,
    convert_keys: bool = True,
) -> Tuple[Optional[List[str]], Optional[str]]:
    """Build the tmux command that opens an ssh session.

    Returns (argv, temp_key_path) where argv is a list safe to pass to
    subprocess.run(shell=False). With tmux=False, argv is the bare ssh
    command (used for clipboard copy / dry-run display / no-tmux mode).

    quiet=True suppresses status prints (preview mode). With
    convert_keys=False, PublicKeyFile is shown as a placeholder without
    running puttygen - no filesystem side effects (TUI preview).
    Returns (None, None) if the session has no usable HostName.
    """
    if "HostName" not in session or not session["HostName"].strip():
        return None, None

    temp_key: Optional[str] = None
    proto = session.get("Protocol", "ssh").strip().lower()

    if proto == "mosh":
        # Mosh session: user@host via mosh; port/key options mostly
        # handled by mosh-server negotiation, so we pass just identity.
        parts = ["mosh"]
        user = session.get("UserName", "").strip()
        host = session["HostName"].strip()
        ssh_port = session.get("PortNumber", "22")
        ssh_opts = []
        if ssh_port != "22":
            ssh_opts += ["-p", ssh_port]
        ppk = session.get("PublicKeyFile", "").strip()
        if ppk:
            if convert_keys:
                converted, temp_key = _convert_session_key(ppk, reg_path, quiet=quiet)
                if converted:
                    ssh_opts += ["-i", converted]
            elif not quiet:
                print("  → Using key: (cached on connect)")
        if ssh_opts:
            parts.append("--ssh=" + shlex.join(["ssh"] + ssh_opts))
        parts.append(f"{user}@{host}" if user else host)
        # fall through to the shared tmux/log wrapper below
    else:
        parts, temp_key = _build_ssh_argv(
            session, reg_path, quiet=quiet, convert_keys=convert_keys)

    if not tmux:
        # Bare argv (clipboard copy, dry-run display, no-tmux mode).
        # Skip log-dir makedirs side effect.
        return parts, temp_key

    return _wrap_tmux(parts, session_name, reg_path), temp_key


def _build_ssh_argv(
    session: Dict[str, str], reg_path: Optional[Path],
    quiet: bool = False, convert_keys: bool = True,
) -> Tuple[List[str], Optional[str]]:
    """Build the plain ssh argv for an ssh-protocol session.

    Returns (parts, temp_key). quiet/convert_keys pass through to the
    key conversion for side-effect-free preview builds."""
    temp_key: Optional[str] = None
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
    elif proxy_method in {"1", "2", "3"} and proxy_host and not quiet:
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

    ppk_file_str = session.get("PublicKeyFile", "").strip()
    if ppk_file_str:
        if convert_keys:
            converted, temp_key = _convert_session_key(
                ppk_file_str, reg_path, quiet=quiet)
            if converted:
                parts.extend(["-i", converted])
        elif not quiet:
            print("  -> Using key: (converted on connect)")

    remote_cmd = session.get("RemoteCommand", "").strip()
    if remote_cmd:
        parts.insert(1, "-t")
        parts.append(remote_cmd)

    # Extra ssh args from PuTTY's CommandLineArgs session key
    # (Connection > SSH > Command line "Remote command/arguments" box).
    # Shlex-split so multi-word options arrive as separate argv tokens.
    extra_args = session.get("CommandLineArgs", "").strip()
    if extra_args:
        try:
            parts.extend(shlex.split(extra_args))
        except ValueError as e:
            print(f"  → Warning: ignoring malformed CommandLineArgs: {e}",
                  file=sys.stderr)

    return parts, temp_key


def _wrap_tmux(
    parts: List[str], session_name: str, reg_path: Optional[Path],
) -> List[str]:
    """Wrap an argv in a tmux launch that pipes the pane to a log file.

    tmux accepts commands separated by ';' as plain argv tokens, so no
    shell is invoked - injection-safe by construction.

    The session/window is created ATTACHED (no -d): the tmux client blocks
    until the pane command exits, so putty.py's subprocess.run waits for
    the ssh session naturally. (Creating detached + attaching later is
    unreliable on tmux 3.2+: an unattached session with
    destroy-unattached is reaped before the attach can run.)

    Outside tmux: new-session, explicitly named (-s), with
    destroy-unattached set so a manual detach cleans it up.
    Inside tmux: new-window.
    """
    timestamp = time.strftime("%Y-%m-%d_%H%M")
    log_dir = os.environ.get("PUTTY_LOG_DIR")
    if not log_dir:
        if reg_path is not None:
            log_dir = str(reg_path.parent)
        else:
            log_dir = str(Path.home() / ".putty-cli" / "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = f"{log_dir}/{timestamp}-{safe_name(session_name)}.log"

    in_tmux = bool(os.environ.get("TMUX"))
    name = safe_name(session_name)
    argv = ["tmux"]
    if in_tmux:
        argv += ["new-window", "-n", name,
                 " ".join(shlex.quote(a) for a in parts), ";"]
    else:
        argv += ["new-session", "-s", name, "-n", name,
                 " ".join(shlex.quote(a) for a in parts), ";",
                 "set-option", "-t", name, "destroy-unattached", "on", ";"]
    argv += ["pipe-pane", "-t", name, "-o", f"cat >> {shlex.quote(log_file)}"]
    return argv
