import os
import re
import shlex
import subprocess  # nosec
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .ppk_convert_util import convert_ppk_to_openssh
from .session_util import jump_host_of

# Root under which Windows drives appear (WSL). Injectable for tests.
WSL_MNT_ROOT = Path("/mnt")

#: a Windows drive-letter path like C:\Users\me\keys
_WIN_PATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def reg_friendly_path(picked: Path,
                      reg_dir: Optional[Path] = None) -> str:
    """Store a picked key file the way BOTH PuTTY and putty-cli resolve.

    - directly in the .reg export's directory -> bare filename
      (portable PuTTY style; putty-cli resolves it relative to the .reg)
    - under the WSL mount of a Windows drive -> the C:\\... form
      (PuTTY's native path; putty-cli maps it back under /mnt)
    - anywhere else -> the absolute Linux path
    """
    if reg_dir is not None:
        try:
            rel = picked.resolve().relative_to(reg_dir.resolve())
        except (ValueError, OSError):
            pass
        else:
            if len(rel.parts) == 1:
                return rel.parts[0]
    parts = picked.resolve().parts
    # under the WSL mount of a single drive letter: /mnt/c/... -> C:\...
    root = WSL_MNT_ROOT.parts
    if len(parts) > len(root) and parts[:len(root)] == root \
            and len(parts[len(root)]) == 1:
        drive = parts[len(root)].upper()
        return f"{drive}:\\" + "\\".join(parts[len(root) + 1:])
    return str(picked)


def browsable_key_dir(value: str,
                      reg_dir: Optional[Path]) -> Optional[Path]:
    """Where to open the key picker, from the current Key field value:

    C:\\... maps back under the WSL mount; an absolute path uses its
    parent; a relative name means the .reg's directory (or a subdir of
    it); an empty value starts at reg_dir. None lets the caller pick
    its own fallback."""
    v = (value or "").strip()
    m = _WIN_PATH_RE.match(v)
    if m:
        rest = m.group(2).replace("\\", "/")
        mapped = WSL_MNT_ROOT / m.group(1).lower() / rest if rest \
            else WSL_MNT_ROOT / m.group(1).lower()
        return mapped.parent
    if v.startswith("/"):
        return Path(v).parent
    if v:
        base = reg_dir if reg_dir is not None else Path.home()
        return (base / v).parent
    return reg_dir


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


def log_dir(reg_path: Optional[Path] = None) -> Path:
    """Where session logs live (pure path resolution):
    $PUTTY_LOG_DIR > the .reg file's directory > ~/.putty-cli/logs."""
    return Path(os.environ.get("PUTTY_LOG_DIR")
                or (reg_path.parent if reg_path is not None
                    else Path.home() / ".putty-cli" / "logs")).expanduser()


def _plan_ssh(session: Dict[str, str],
              convert_keys: bool = True,
              ) -> Tuple[List[str], Optional[str], List[str]]:
    """PURE ssh-protocol plan: (argv, ppk_file, notes).

    The argv carries a '' placeholder where the identity file goes -
    the caller splices the converted key path in, or drops the
    ["-i", ""] pair when no key is usable. Notes are warning/status
    lines for the caller to print. No I/O, no prints, no subprocesses."""
    parts = ["ssh"]
    notes: List[str] = []

    port = session.get("PortNumber", "22")
    if port != "22":
        parts.extend(["-p", port])

    # ProxyMethod 6 is PuTTY's "local" proxy command type. This tool maps it
    # to ssh's ProxyJump (-J): in practice PuTTY setups exported here use
    # ProxyHost/ProxyPort/ProxyUsername of a jump host, which is exactly
    # what -J models. Methods 1-3 (SOCKS4/5, HTTP CONNECT) are not supported
    # and produce a warning note below.
    jump = jump_host_of(session)
    if jump:
        proxy_port = session.get("ProxyPort", "22").strip()
        proxy_user = session.get("ProxyUsername", "").strip()
        if proxy_user:
            jump = f"{proxy_user}@{jump}"
        if proxy_port and proxy_port != "22":
            jump = f"{jump}:{proxy_port}"
        parts.extend(["-J", jump])
    elif session.get("ProxyMethod", "0").strip() in {"1", "2", "3"} \
            and session.get("ProxyHost", "").strip():
        notes.append(
            f"  → Warning: proxy type "
            f"{session.get('ProxyMethod', '0').strip()} (SOCKS/HTTP) has "
            "no direct ssh equivalent; ignoring proxy for "
            f"{session.get('ProxyHost', '').strip()}. "
            "Consider ProxyMethod 6 (jump host) in PuTTY instead.")

    user = session.get("UserName", "").strip()
    host = session["HostName"].strip()
    parts.append(f"{user}@{host}" if user else host)

    ppk = session.get("PublicKeyFile", "").strip() or None
    if ppk:
        parts.extend(["-i", ""])   # identity-file placeholder
        if not convert_keys:
            notes.append("  -> Using key: (converted on connect)")

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
            notes.append(
                f"  → Warning: ignoring malformed CommandLineArgs: {e}")

    return parts, ppk, notes


def _plan_mosh(session: Dict[str, str], convert_keys: bool = True,
               ) -> Tuple[List[str], List[str], Optional[str], List[str]]:
    """PURE mosh-protocol plan: (argv, ssh_opts, ppk_file, notes).

    Mosh negotiates port/key server-side via its ssh bootstrap, so the
    plan returns the ssh options separately; ssh_opts carry the
    identity-file placeholder ('') - the caller resolves the key and
    then joins them into mosh's --ssh= option."""
    notes: List[str] = []
    ssh_opts: List[str] = []
    port = session.get("PortNumber", "22")
    if port != "22":
        ssh_opts += ["-p", port]
    ppk = session.get("PublicKeyFile", "").strip() or None
    if ppk:
        ssh_opts += ["-i", ""]   # identity-file placeholder
        if not convert_keys:
            notes.append("  → Using key: (cached on connect)")
    user = session.get("UserName", "").strip()
    host = session["HostName"].strip()
    parts = ["mosh", f"{user}@{host}" if user else host]
    return parts, ssh_opts, ppk, notes


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

    The argv itself comes from the pure planners (_plan_ssh /
    _plan_mosh); the effects live only here: printing the planners'
    notes, running puttygen when convert_keys=True (quiet=True
    suppresses output), and creating the log dir for the tmux wrap.
    With convert_keys=False, PublicKeyFile is left out of the argv
    without running puttygen - no filesystem side effects (TUI
    preview). Returns (None, None) if the session has no usable
    HostName.
    """
    if "HostName" not in session or not session["HostName"].strip():
        return None, None

    proto = session.get("Protocol", "ssh").strip().lower()
    temp_key: Optional[str] = None
    if proto == "mosh":
        parts, ssh_opts, ppk, notes = _plan_mosh(session, convert_keys)
    else:
        parts, ppk, notes = _plan_ssh(session, convert_keys)
        ssh_opts = None
    if not quiet:
        for note in notes:
            print(note, file=sys.stderr)

    if ppk:
        holder = ssh_opts if ssh_opts is not None else parts
        idx = holder.index("")
        if convert_keys:
            converted, temp_key = _convert_session_key(
                ppk, reg_path, quiet=quiet)
            if converted:
                holder[idx] = converted   # splice the real key path in
                idx = -1                  # nothing to drop
        if idx >= 0:
            del holder[idx - 1:idx + 1]   # no usable key: drop "-i", ""
    if ssh_opts:
        parts.insert(1, "--ssh=" + shlex.join(["ssh"] + ssh_opts))

    if not tmux:
        # Bare argv (clipboard copy, dry-run display, no-tmux mode).
        # Skip log-dir makedirs side effect.
        return parts, temp_key

    return _wrap_tmux(parts, session_name, reg_path), temp_key


def _unique_tmux_name(name: str) -> str:
    """Disambiguate a tmux session name against live sessions:
    safe_name() flattens 'Foo Bar' and 'Foo/Bar' to the same 'Foo_Bar',
    and new-session refuses a duplicate name. Appends -2, -3, ... until
    the name is free. '-t =name' is the EXACT-match form - a plain -t
    target parses dots as window/pane separators (see the note in
    _wrap_tmux's docstring). Best-effort: if tmux can't be queried the
    name is returned unchanged (the launch will surface the error)."""
    candidate = name
    for i in range(1, 21):
        try:
            result = subprocess.run(  # nosec B603, B607: fixed argv
                ["tmux", "has-session", "-t", f"={candidate}"],
                capture_output=True)
        except OSError:
            return candidate
        if result.returncode != 0:    # no such session: name is free
            return candidate
        candidate = f"{name}-{i + 1}"
    return candidate


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

    pipe-pane and set-option run TARGETLESS: they resolve to the pane
    just created and selected. NOTE: '-t <name>' targets DO NOT WORK
    for session names containing a dot - tmux parses 'foo.com' as
    window 'foo' + pane 'com' ("can't find pane: com") and the session
    silently loses its log. (Names are safe in -s/-n creation, only
    target parsing is affected.)
    """
    timestamp = time.strftime("%Y-%m-%d_%H%M")
    d = log_dir(reg_path)
    os.makedirs(d, exist_ok=True)
    log_file = f"{d}/{timestamp}-{safe_name(session_name)}.log"

    in_tmux = bool(os.environ.get("TMUX"))
    name = safe_name(session_name)
    argv = ["tmux"]
    if in_tmux:
        argv += ["new-window", "-n", name,
                 " ".join(shlex.quote(a) for a in parts), ";"]
    else:
        # new-session REFUSES a duplicate -s name (two sessions that
        # flatten to the same safe_name would collide), so -s gets a
        # live-session-unique name. -n and the log filename keep the
        # raw safe_name: window names are auto-uniquified by tmux, and
        # the log file must keep matching previously written logs.
        session_name_arg = _unique_tmux_name(name)
        argv += ["new-session", "-s", session_name_arg, "-n", name,
                 " ".join(shlex.quote(a) for a in parts), ";",
                 "set-option", "destroy-unattached", "on", ";"]
    argv += ["pipe-pane", "-o", f"cat >> {shlex.quote(log_file)}"]
    return argv
