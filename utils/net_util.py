"""Network reachability probes shared by the CLI menu and the TUI."""
import re
import shutil
import socket
import subprocess  # nosec
from typing import List, Tuple

PING_TIME_RE = re.compile(r"time[=<]([\d.]+)\s*ms", re.IGNORECASE)


def _tcp_probe(host: str, port: str = "22") -> Tuple[bool, str]:
    """TCP connect probe (last-resort reachability check)."""
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            return True, f"{host}:{port} reachable (tcp)"
    except OSError as e:
        return False, f"{host}:{port} unreachable (tcp: {e})"


def interpret_ping_output(host: str, returncode: int, stdout: str,
                          stderr: str) -> Tuple[bool, bool, str]:
    """PURE classifier for one finished system-ping run.

    Returns (ok, needs_tcp_fallback, message). needs_tcp_fallback is
    True when the ping binary exists but lacks raw-socket permission
    (containers, some WSL setups) - the caller should then try the
    TCP-port probe instead of reporting failure."""
    out = f"{stdout} {stderr}"
    err = out.lower()
    if returncode == 0:
        m = PING_TIME_RE.search(out)
        if m:
            return True, False, f"{host}: {m.group(1)} ms"
        return True, False, f"{host}: reachable"
    if "operation not permitted" in err:
        return False, True, ""
    if "unknown host" in err or "name or service not known" in err \
            or "no address associated" in err:
        return False, False, f"{host}: unknown host"
    if "unreachable" in err:
        return False, False, f"{host}: unreachable"
    if "100% packet loss" in err or "packet loss" in err:
        return False, False, f"{host}: unreachable (no reply)"
    return False, False, f"{host}: unreachable (ping exit {returncode})"


def run_ping(host: str, port: str = "22") -> Tuple[bool, str]:
    """Reachability check that behaves like a real ping first: one ICMP
    echo via the system ping binary, reporting round-trip latency.

    Falls back to a TCP-port connect probe when ping is missing, not
    permitted, or otherwise unusable. Returns (ok, message)."""
    host = host.split("@")[-1].strip()
    if not host:
        return False, "no hostname to ping"

    ping_bin = shutil.which("ping")
    if ping_bin:
        proc = None
        try:
            proc = subprocess.run(  # nosec: argv list, no shell
                [ping_bin, "-c", "1", "-W", "2", host],
                capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None:
            ok, needs_tcp, msg = interpret_ping_output(
                host, proc.returncode, proc.stdout or "",
                proc.stderr or "")
            if needs_tcp:
                return _tcp_probe(host, port)  # ping lacks raw-socket caps
            return ok, msg

    return _tcp_probe(host, port)


def run_jump_probe(argv: List[str], jump: str) -> Tuple[bool, str]:
    """Reachability probe THROUGH the jump host, using the real ssh
    argv the connect would run (the bare ssh command from
    build_ssh_command with tmux=False - it carries -J, identity file,
    port and user). Returns (ok, message).

    BatchMode=yes is ESSENTIAL: the probe has no TTY, and without it
    a password-prompting host would hang until the timeout."""
    if not argv:
        return False, "no command to probe"
    probe = argv + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                    "-n", "true"]
    try:
        proc = subprocess.run(  # nosec: argv from our own builder, no shell
            probe, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return False, "ssh not found - cannot probe via jump host"
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"unreachable via {jump} ({e})"
    if proc.returncode == 0:
        return True, f"reachable via {jump}"
    lines = [l for l in (proc.stderr or "").splitlines() if l.strip()] \
        or [l for l in (proc.stdout or "").splitlines() if l.strip()]
    detail = lines[-1] if lines else f"ssh exit {proc.returncode}"
    return False, f"unreachable via {jump} ({detail})"