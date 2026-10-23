import re
from pathlib import Path
from typing import Dict, Optional, Tuple

SESSION_HEADER_RE = re.compile(
    r'^\[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\(.+?)\]$',
    re.IGNORECASE,
)
VALUE_RE = re.compile(r'^"([^"]+)"=(.+)$')


def file_stamp(path: Path) -> Optional[Tuple[int, int]]:
    """(size, mtime_ns) of a file, or None if unreadable - the cheap
    staleness stamp for the menu's reload-on-change loop (pure)."""
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _read_reg_text(file_path: Path) -> str:
    """Read a .reg file: BOM-sniff UTF-16, detect BOM-less UTF-16LE (null
    bytes early in the file), otherwise treat as UTF-8."""
    data = file_path.read_bytes()
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16", errors="replace")
    if data[:2] == b"\x00[" or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16-be", errors="replace")
    if b"\x00" in data[:256]:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _parse_content(content: str) -> Dict[str, Dict[str, str]]:
    """Parse already-decoded .reg text into {session_name: {key: value}}.

    (Exposed separately so write-back validation can parse in-memory
    text without touching a file.)"""
    content = content.replace("\r\n", "\n").lstrip("\ufeff")

    sessions: Dict[str, Dict[str, str]] = {}
    current_session = None

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        m = SESSION_HEADER_RE.match(line)
        if m:
            session_name = m.group(1).strip()
            sessions[session_name] = {}
            current_session = session_name
            continue

        if current_session is None:
            continue

        m_val = VALUE_RE.match(line)
        if m_val:
            key = m_val.group(1).strip()
            val_raw = m_val.group(2).strip()

            if val_raw.startswith('"') and val_raw.endswith('"'):
                val = val_raw[1:-1].replace('\\\\', '\\')
            elif val_raw.startswith("dword:"):
                try:
                    val = str(int(val_raw[6:], 16))
                except ValueError:
                    val = "0"
            else:
                val = val_raw

            sessions[current_session][key] = val

    return sessions


def parse_putty_reg(file_path: Path) -> Dict[str, Dict[str, str]]:
    """Parse a PuTTY .reg export into {session_name: {key: value}}.

    Raises FileNotFoundError if the file does not exist, and OSError
    (or ValueError) if it cannot be read.
    """
    if not file_path.is_file():
        raise FileNotFoundError(f".reg file not found: {file_path}")

    return _parse_content(_read_reg_text(file_path))


def _snapshot_paths() -> Tuple[Path, Path]:
    """(snapshot .reg copy, stamp file) under ~/.cache/putty-cli/."""
    cache = Path.home() / ".cache" / "putty-cli"
    return (cache / "reg-snapshot.reg", cache / "reg-snapshot.stamp")


def parse_putty_reg_cached(file_path: Path) -> Dict[str, Dict[str, str]]:
    """parse_putty_reg via a LOCAL snapshot when one is fresh.

    The .reg typically lives on /mnt/c (WSL's 9P filesystem, often under
    OneDrive): a cold full read can take seconds, while a stat is a
    single metadata hop. The snapshot refreshes from the source whenever
    its (size, mtime_ns) stamp changes, so callers get the same
    freshness at the cost of one stat. Any snapshot trouble falls back
    to reading the source directly (same errors as parse_putty_reg).
    """
    snapshot, stamp_file = _snapshot_paths()
    stamp = file_stamp(file_path)   # None when missing/unreadable
    try:
        if stamp is not None:
            saved = stamp_file.read_text(encoding="utf-8").strip()
            if saved == f"{file_path.resolve()}|{stamp[0]}|{stamp[1]}" \
                    and snapshot.is_file():
                return _parse_content(_read_reg_text(snapshot))
    except OSError:
        pass  # no/Corrupt snapshot: fall through to the real read

    sessions = parse_putty_reg(file_path)   # the slow path, only here
    try:  # best-effort snapshot refresh; never fatal
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        tmp = snapshot.with_name(snapshot.name + ".tmp")
        tmp.write_bytes(file_path.read_bytes())
        tmp.replace(snapshot)
        if stamp is not None:
            stamp_file.write_text(
                f"{file_path.resolve()}|{stamp[0]}|{stamp[1]}\n",
                encoding="utf-8")
    except OSError:
        pass
    return sessions
